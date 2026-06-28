"""Advanced reasoning delegation for the Agent Team."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any

from ..config import Config
from .agent_team_service import (
    AGENT_TEAM_PROVIDERS,
    agent_team_confirm_prompt,
    agent_team_member_for,
    agent_team_member_mode,
    agent_team_member_requires_external_approval,
    agent_team_member_settings,
    agent_team_notify,
    config_get,
    config_set,
)
logger = logging.getLogger(__name__)

ADVANCED_REASONING_MEMBER_KEY = "advanced_reasoning"

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
    return agent_team_member_for(config, ADVANCED_REASONING_MEMBER_KEY) is not None


def advanced_reasoning_provider(config: Any) -> str:
    member = agent_team_member_settings(config, ADVANCED_REASONING_MEMBER_KEY)
    return str(member.get("provider") or "openai").strip().lower()


def advanced_reasoning_model(config: Any) -> str:
    member = agent_team_member_settings(config, ADVANCED_REASONING_MEMBER_KEY)
    return str(member.get("model") or "gpt-4o").strip()


def advanced_reasoning_confirm_prompt(config: Any) -> bool:
    return agent_team_confirm_prompt(config)


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
    return agent_team_notify(config)


def advanced_reasoning_effort(config: Any) -> str:
    return agent_team_member_mode(config, ADVANCED_REASONING_MEMBER_KEY, "medium")


def apply_advanced_reasoning_mode(
    config: Any,
    *,
    provider: str,
    model: str,
    client: Any = None,
) -> str:
    from .llm_model_catalog import default_llm_mode_for_options, reasoning_effort_options_for_model

    options = reasoning_effort_options_for_model(provider, model)
    if not options:
        return ""

    effort = advanced_reasoning_effort(config)
    if effort not in options:
        effort = default_llm_mode_for_options(options)

    provider_id = str(provider or "").strip().lower()
    if provider_id == "openai":
        config_set(config, "openai.reasoning_effort", effort)
    elif provider_id == "codex-cli":
        config_set(config, "codex_cli.reasoning_effort", effort)
    elif provider_id == "claude-cli":
        config_set(config, "claude_cli.reasoning_effort", effort)
    elif client is not None and hasattr(client, "set_llm_mode"):
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

    def __init__(self, config: Config):
        self.config = config

    def is_enabled(self) -> bool:
        return advanced_reasoning_enabled(self.config)

    def _provider(self) -> str:
        return advanced_reasoning_provider(self.config)

    def _model(self) -> str:
        return advanced_reasoning_model(self.config)

    async def run(self, prompt: str, *, redacted_prompt: str = "") -> str:
        if not self.is_enabled():
            return "Advanced reasoning is disabled in settings."

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return "Advanced reasoning request is empty."

        provider = self._provider()
        model = self._model()
        if provider not in AGENT_TEAM_PROVIDERS:
            return f"Unsupported advanced reasoning provider: {provider}"
        if not model:
            return "Advanced reasoning model is not configured."

        member = agent_team_member_for(self.config, ADVANCED_REASONING_MEMBER_KEY)
        if not agent_team_member_requires_external_approval(member):
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
            response = client.responses.create(**kwargs)
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
