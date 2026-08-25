"""Advanced reasoning delegation for the Agent Team."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from typing import Any, Mapping, Optional

from ..config import Config
from .agent_team_service import (
    AGENT_TEAM_PROVIDERS,
    config_get,
    config_set,
)
from .agent_team_v3 import resolve_agent_team_v3_route, subagent_requires_external_approval
from .turn_context import get_turn_context

logger = logging.getLogger(__name__)

# This module is retained only as an import-compatible, hard-disabled shim.
# Advanced Reasoning is not a Team/Subagent runtime path in schema v3.  Keep a
# stable identifier for callers that still ask for provider/model metadata, but
# never route through the removed v2 topology helpers.
ADVANCED_REASONING_SUBAGENT_ID = "advanced_reasoning"
# Phase 7 removes Advanced Reasoning from the runtime/tool graph.  Keep this
# module as a compatibility shim for old imports, but fail closed at every
# execution entry point so importing it can never send externally.
ADVANCED_REASONING_RUNTIME_DISABLED = True


# Importing ``src.llm.conversation_context`` at module import time pulls in the
# ``src.llm`` package initializer, which imports the manager and this service's
# tool registry in return.  Keep telemetry helpers lazy so direct imports of
# this service remain cycle-free; tests and callers can still monkeypatch these
# module-level names as before.
def normalize_usage(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from ..llm.conversation_context import normalize_usage as _normalize_usage

    return _normalize_usage(*args, **kwargs)


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    from ..llm.conversation_context import persist_usage_sync as _persist_usage_sync

    return bool(_persist_usage_sync(*args, **kwargs))

_SECRET_VALUE_PATTERN = re.compile(
    r"(?P<prefix>\b[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|TOKEN|SECRET|PASSWORD|PASS|KEY)\b\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\"'\s,;]{8,})"
    r"(?P=quote)",
    re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(
    r"(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]{20,})",
    re.IGNORECASE,
)
_API_TOKEN_PATTERN = re.compile(
    r"\b(?:sk-proj|sk|sk-ant|ghp|github_pat|glpat|hf|xoxb|xoxp|xoxa|xoxr)-[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_INTERNAL_URL_PATTERN = re.compile(
    r"\bhttps?://(?:"
    r"localhost|127(?:\.\d{1,3}){3}|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
    r"[^/\s:]+(?:\.local|\.internal|\.corp|\.lan|\.intra)"
    r")(?::\d+)?[^\s<>\"]*",
    re.IGNORECASE,
)
_PRIVATE_IP_PATTERN = re.compile(
    r"\b(?:127(?:\.\d{1,3}){3}|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
_WINDOWS_PATH_PATTERN = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\":|?*]+")
_UNIX_USER_PATH_PATTERN = re.compile(r"(?<!\w)/(?:Users|home|mnt|srv|var)/(?:[^\s<>\"']+)")


def advanced_reasoning_enabled(config: Any) -> bool:
    return False


def advanced_reasoning_provider(config: Any) -> str:
    route = resolve_agent_team_v3_route(config, ADVANCED_REASONING_SUBAGENT_ID) or {}
    return str(route.get("provider") or config_get(config, "llm_provider", "openai") or "openai").strip().lower()


def advanced_reasoning_model(config: Any) -> str:
    route = resolve_agent_team_v3_route(config, ADVANCED_REASONING_SUBAGENT_ID) or {}
    return str(route.get("model") or config_get(config, "llm_model", "") or "").strip()


def advanced_reasoning_confirm_prompt(config: Any) -> bool:
    # Legacy readers only; runtime execution is disabled above.
    return bool(config_get(config, "agent_team.confirm_prompt", True))


def advanced_reasoning_confirmation_enabled(config: Any) -> bool:
    if _current_generation_policy_auto_approves():
        return False
    return advanced_reasoning_confirm_prompt(config)


def _current_generation_policy_auto_approves() -> bool:
    try:
        from ..llm.generation_policy import (
            PermissionPolicy,
            get_current_generation_policy,
        )

        return (
            get_current_generation_policy().permission_policy
            == PermissionPolicy.AUTO_APPROVE
        )
    except Exception:
        return False


def advanced_reasoning_notify(config: Any) -> bool:
    return bool(config_get(config, "agent_team.notify", True))


def advanced_reasoning_effort(config: Any) -> str:
    route = resolve_agent_team_v3_route(config, ADVANCED_REASONING_SUBAGENT_ID) or {}
    return str(route.get("effort") or route.get("reasoning_effort") or "").strip()


def apply_advanced_reasoning_mode(
    config: Any,
    *,
    provider: str,
    model: str,
    client: Any = None,
) -> str:
    from .llm_model_catalog import (
        reasoning_effort_default_for_model,
        reasoning_effort_options_for_model,
    )
    from ..llm.openai_compatible_local_profiles import llama_cpp_reasoning_effort_metadata

    options = reasoning_effort_options_for_model(provider, model)
    if not options:
        return ""

    route = resolve_agent_team_v3_route(config, ADVANCED_REASONING_SUBAGENT_ID) or {}
    effort = str(route.get("effort") or route.get("reasoning_effort") or "").strip()
    requested_effort = str(route.get("requested_reasoning_effort") or "").strip()
    if (
        requested_effort
        and provider == "openai_compatible_local"
        and llama_cpp_reasoning_effort_metadata(model)
    ):
        raise ValueError(
            f"Unsupported reasoning effort for {provider}/{model}: {requested_effort}"
        )
    if not effort:
        effort = str(reasoning_effort_default_for_model(provider, model) or "").strip()
    if effort and effort not in options:
        # Managed Qwen profiles must never silently map unsupported values to
        # fast/thinking or another effort.  Other providers retain their
        # existing route-resolution compatibility behaviour.
        if llama_cpp_reasoning_effort_metadata(model) and provider == "openai_compatible_local":
            raise ValueError(
                f"Unsupported reasoning effort for {provider}/{model}: {effort}"
            )
        return ""

    if effort and client is not None and hasattr(client, "set_llm_mode"):
        client.set_llm_mode(effort)
    return effort


def _redaction_placeholder(category: str, counters: dict[str, int], findings: list[dict[str, str]]) -> str:
    counters[category] = counters.get(category, 0) + 1
    placeholder = f"[{category}_{counters[category]}]"
    findings.append({"category": category, "placeholder": placeholder})
    return placeholder


def _apply_value_pattern(
    text: str,
    pattern: re.Pattern[str],
    category: str,
    counters: dict[str, int],
    findings: list[dict[str, str]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{_redaction_placeholder(category, counters, findings)}"

    return pattern.sub(replace, text)


def _apply_plain_pattern(
    text: str,
    pattern: re.Pattern[str],
    category: str,
    counters: dict[str, int],
    findings: list[dict[str, str]],
) -> str:
    return pattern.sub(
        lambda _match: _redaction_placeholder(category, counters, findings),
        text,
    )


def _configured_redaction_terms(config: Any) -> list[str]:
    raw_terms = config_get(config, "agent_team.redaction_terms", [])
    if not isinstance(raw_terms, list):
        return []
    terms = []
    for term in raw_terms:
        value = str(term or "").strip()
        if value:
            terms.append(value)
    return terms


def build_redacted_prompt(
    original_prompt: str,
    *,
    proposed_redacted_prompt: str = "",
    config: Any = None,
) -> tuple[str, list[dict[str, str]]]:
    """Return the default outbound text for an external-model prompt."""
    base_prompt = (proposed_redacted_prompt or "").strip() or original_prompt
    findings: list[dict[str, str]] = []
    counters: dict[str, int] = {}
    redacted = base_prompt

    redacted = _apply_value_pattern(redacted, _SECRET_VALUE_PATTERN, "SECRET", counters, findings)
    redacted = _apply_value_pattern(redacted, _BEARER_TOKEN_PATTERN, "SECRET", counters, findings)
    for pattern, category in [
        (_API_TOKEN_PATTERN, "SECRET"),
        (_AWS_ACCESS_KEY_PATTERN, "SECRET"),
        (_JWT_PATTERN, "SECRET"),
        (_INTERNAL_URL_PATTERN, "INTERNAL_URL"),
        (_EMAIL_PATTERN, "EMAIL"),
        (_PRIVATE_IP_PATTERN, "INTERNAL_HOST"),
        (_WINDOWS_PATH_PATTERN, "LOCAL_PATH"),
        (_UNIX_USER_PATH_PATTERN, "LOCAL_PATH"),
    ]:
        redacted = _apply_plain_pattern(redacted, pattern, category, counters, findings)

    for term in _configured_redaction_terms(config):
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        redacted = _apply_plain_pattern(redacted, pattern, "CONFIDENTIAL_TERM", counters, findings)

    return redacted, findings


def clone_config(config: Config | dict[str, Any]) -> Config | dict[str, Any]:
    if isinstance(config, dict):
        return copy.deepcopy(config)
    cloned = copy.copy(config)
    cloned.config = copy.deepcopy(config.config)
    return cloned


class AdvancedReasoningService:
    """Runs a tool-free advanced reasoning subagent after optional user approval."""

    class _UsageClient:
        def __init__(
            self,
            *,
            user_id: Optional[str],
            session_id: Optional[str],
            project_id: Optional[str],
            agent_name: Optional[str],
        ) -> None:
            self.current_session_id = session_id
            self.current_project_id = project_id
            self.character_name = agent_name
            self._user_id = user_id

        def _get_session_user_id(self) -> str:
            return str(self._user_id or "default_user")

    @staticmethod
    def _as_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        for method_name in ("model_dump", "to_dict", "dict"):
            method = getattr(value, method_name, None)
            if callable(method):
                try:
                    dumped = method()
                except Exception:
                    continue
                if isinstance(dumped, Mapping):
                    return {str(key): item for key, item in dumped.items()}
        raw = getattr(value, "__dict__", None)
        if isinstance(raw, Mapping):
            return {
                str(key): item
                for key, item in raw.items()
                if not str(key).startswith("_")
            }
        return {}

    @classmethod
    def _response_usage(cls, response: Any) -> dict[str, Any]:
        raw = getattr(response, "usage", None)
        if raw is None and isinstance(response, Mapping):
            raw = response.get("usage")
        if raw is None:
            return {}
        usage = normalize_usage(cls._as_mapping(raw), provider="openai")
        if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
            return {}
        resolved_model = getattr(response, "model", None)
        if resolved_model is None and isinstance(response, Mapping):
            resolved_model = response.get("model")
        if resolved_model and "resolved_model" not in usage:
            usage["resolved_model"] = str(resolved_model)
        return usage

    def __init__(
        self,
        config: Config,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        agent_name: str = "advanced_reasoning",
        request_type: str = "advanced_reasoning",
    ):
        self.config = config
        self.user_id = user_id
        self.session_id = session_id
        self.project_id = project_id
        self.agent_name = agent_name
        self.request_type = request_type
        self._recorded_usage_responses: list[Any] = []

    def set_usage_context(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        request_type: Optional[str] = None,
    ) -> None:
        if user_id is not None:
            self.user_id = str(user_id) if user_id else None
        if session_id is not None:
            self.session_id = str(session_id) if session_id else None
        if project_id is not None:
            self.project_id = str(project_id) if project_id else None
        if agent_name:
            self.agent_name = str(agent_name)
        if request_type:
            self.request_type = str(request_type)

    def _usage_identity(self) -> dict[str, Optional[str]]:
        """Resolve the request identity without mutating shared turn state."""

        turn = get_turn_context()
        return {
            "user_id": self.user_id or turn.user_id,
            "session_id": self.session_id or turn.session_id,
            "project_id": self.project_id or turn.project_id,
            "agent_name": self.agent_name,
        }

    def _apply_client_usage_context(self, client: Any) -> None:
        """Apply this invocation's identity to a short-lived provider client.

        ``create_llm_client`` returns a fresh client for non-OpenAI routes, but
        the provider's own usage recorder reads the client's session fields.
        Keep all assignments scoped to that fresh instance and tolerate clients
        that expose only a subset of the context API.
        """

        if client is None:
            return
        identity = self._usage_identity()
        user_id = identity.get("user_id")
        try:
            setter = getattr(client, "set_session_context", None)
            if callable(setter) and user_id:
                setter(user_id=str(user_id))
            elif user_id and hasattr(client, "session_user_id"):
                setattr(client, "session_user_id", str(user_id))
        except Exception:
            logger.debug(
                "Advanced reasoning temporary client user context setup failed",
                exc_info=True,
            )

        agent_name = identity.get("agent_name")
        if agent_name and hasattr(client, "character_name"):
            try:
                setattr(client, "character_name", str(agent_name))
            except Exception:
                logger.debug(
                    "Advanced reasoning temporary client agent context setup failed",
                    exc_info=True,
                )

        for attribute, value in (
            ("current_session_id", identity.get("session_id")),
            ("current_project_id", identity.get("project_id")),
        ):
            if value is None or not hasattr(client, attribute):
                continue
            try:
                setattr(client, attribute, str(value))
            except Exception:
                logger.debug(
                    "Advanced reasoning temporary client %s context setup failed",
                    attribute,
                    exc_info=True,
                )

    def _record_openai_usage(
        self,
        response: Any,
        *,
        model: str,
        started: float,
    ) -> None:
        try:
            usage = self._response_usage(response)
            if not usage:
                return
            if self._mark_usage_recorded(response):
                return
            identity = self._usage_identity()
            proxy = self._UsageClient(
                user_id=identity.get("user_id"),
                session_id=identity.get("session_id"),
                project_id=identity.get("project_id"),
                agent_name=identity.get("agent_name"),
            )
            persist_usage_sync(
                proxy,
                provider="openai",
                model=str(model),
                requested_model=str(model),
                resolved_model=usage.get("resolved_model"),
                usage=usage,
                request_type=self.request_type,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            )
        except Exception:
            # Telemetry failure must not hide a valid delegated answer.
            logger.debug("Advanced reasoning usage persistence failed", exc_info=True)

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Avoid duplicate rows when a response is replayed by a wrapper."""

        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            setattr(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            recorded = getattr(self, "_recorded_usage_responses", None)
            if recorded is None:
                recorded = []
                self._recorded_usage_responses = recorded
            if any(item is response for item in recorded):
                return True
            recorded.append(response)
            del recorded[:-8]
            return False

    def is_enabled(self) -> bool:
        return advanced_reasoning_enabled(self.config)

    def _provider(self) -> str:
        return advanced_reasoning_provider(self.config)

    def _model(self) -> str:
        return advanced_reasoning_model(self.config)

    def _resolve_deployment_target(
        self,
        provider: str,
        model: str,
    ) -> tuple[str, str]:
        """Resolve advanced reasoning's provider before approval or SDK use."""

        from ..llm.deployment_resolver import (
            preflight_deployment,
            resolve_llm_deployment,
        )

        deployment = resolve_llm_deployment(self.config)
        if deployment is None:
            return provider, model

        configured_route = resolve_agent_team_v3_route(self.config, ADVANCED_REASONING_SUBAGENT_ID)
        if configured_route and configured_route.get("provider") and configured_route.get("model"):
            # A configured Agent Team route is an explicit engine change.  Do
            # not silently rewrite it under a fixed Enterprise backend; reject
            # before sending the approval prompt or making a provider request.
            preflight_deployment(
                self.config,
                provider=provider,
                model=model,
            )
            return provider, model

        available, _ = deployment.provider_available(provider)
        if deployment.fixed or not available:
            return deployment.effective_provider, deployment.effective_model
        return provider, model

    async def run(self, prompt: str, *, redacted_prompt: str = "") -> str:
        if not self.is_enabled():
            return "Advanced reasoning is disabled in settings."

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return "Advanced reasoning request is empty."

        provider, model = self._resolve_deployment_target(
            self._provider(),
            self._model(),
        )
        if provider not in AGENT_TEAM_PROVIDERS:
            return f"Unsupported advanced reasoning provider: {provider}"
        if not model:
            return "Advanced reasoning model is not configured."

        if not subagent_requires_external_approval(self.config, ADVANCED_REASONING_SUBAGENT_ID):
            return await self._run_target_model(clean_prompt, provider=provider, model=model)

        default_prompt, redaction_findings = build_redacted_prompt(
            clean_prompt,
            proposed_redacted_prompt=redacted_prompt,
            config=self.config,
        )
        from ..tools.external_llm_permission import request_external_model_prompt

        approved_prompt = await request_external_model_prompt(
            clean_prompt,
            redacted_prompt=default_prompt,
            redaction_findings=redaction_findings,
            provider=provider,
            model=model,
            description=f"Review the prompt before sending it to {provider}/{model}.",
            confirm=advanced_reasoning_confirmation_enabled(self.config),
            notify=advanced_reasoning_notify(self.config),
            request_kind="advanced_reasoning_assistant",
        )
        if approved_prompt is None:
            return "Advanced reasoning was cancelled."

        return await self._run_target_model(approved_prompt, provider=provider, model=model)

    def run_sync(self, prompt: str, *, redacted_prompt: str = "") -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(prompt, redacted_prompt=redacted_prompt))

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(self.run(prompt, redacted_prompt=redacted_prompt)))
            return future.result(timeout=420)

    async def _run_target_model(self, prompt: str, *, provider: str, model: str) -> str:
        if ADVANCED_REASONING_RUNTIME_DISABLED:
            return "Advanced reasoning is unavailable in this runtime."
        provider, model = self._resolve_deployment_target(provider, model)
        if provider == "openai":
            return await asyncio.to_thread(
                self._run_openai_responses_model,
                prompt,
                model=model,
            )

        from ..llm.manager import create_llm_client

        temp_config = clone_config(self.config)
        config_set(temp_config, "llm_provider", provider)
        config_set(temp_config, "llm_model", model)
        config_set(temp_config, "use_tools", False)
        config_set(temp_config, "skills.enabled", False)
        config_set(temp_config, "memory.enabled", False)
        config_set(temp_config, f"{provider}.model", model)
        apply_advanced_reasoning_mode(temp_config, provider=provider, model=model)

        client = create_llm_client(temp_config)
        apply_advanced_reasoning_mode(temp_config, provider=provider, model=model, client=client)
        self._apply_client_usage_context(client)
        if hasattr(client, "clear_history"):
            client.clear_history()

        try:
            if hasattr(client, "generate_response_async"):
                result = await client.generate_response_async(prompt)
            else:
                result = await asyncio.to_thread(client.generate_response, prompt)
            return str(result or "").strip()
        except Exception as exc:
            logger.exception("[AdvancedReasoningService] delegation failed")
            return f"Advanced reasoning error: {exc}"

    def _run_openai_responses_model(self, prompt: str, *, model: str) -> str:
        if ADVANCED_REASONING_RUNTIME_DISABLED:
            return "Advanced reasoning is unavailable in this runtime."
        try:
            from openai import OpenAI

            client = OpenAI(api_key=config_get(self.config, "openai_api_key"))
            kwargs: dict[str, Any] = {
                "model": model,
                "input": prompt,
            }
            effort = advanced_reasoning_effort(self.config)
            if effort:
                kwargs["reasoning"] = {"effort": effort}
            started = time.perf_counter()
            response = client.responses.create(**kwargs)
            self._record_openai_usage(response, model=model, started=started)
            output_text = str(getattr(response, "output_text", "") or "").strip()
            if output_text:
                return output_text

            parts: list[str] = []
            for item in getattr(response, "output", []) or []:
                for content in getattr(item, "content", []) or []:
                    text = getattr(content, "text", None)
                    if text:
                        parts.append(str(text))
            return "\n".join(parts).strip()
        except Exception as exc:
            logger.exception("[AdvancedReasoningService] OpenAI delegation failed")
            return f"Advanced reasoning error: {exc}"
