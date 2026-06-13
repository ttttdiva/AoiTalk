"""Advanced reasoning delegation guarded by editable external-model approval."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any

from ..config import Config
logger = logging.getLogger(__name__)

ADVANCED_REASONING_PROVIDERS = {
    "openai",
    "openrouter",
    "gemini",
    "ollama",
    "openai_compatible_local",
    "sglang",
    "gemini-cli",
    "claude-cli",
    "codex-cli",
}

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


def config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def config_set(config: Any, key: str, value: Any) -> None:
    if hasattr(config, "set"):
        config.set(key, value)
        return
    if isinstance(config, dict):
        parts = key.split(".")
        target = config
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value


def model_sharing_enabled(config: Any) -> bool:
    return bool(config_get(config, "model_sharing.enabled", False))


def model_sharing_provider(config: Any) -> str:
    return str(config_get(config, "model_sharing.provider", "openai")).strip().lower()


def model_sharing_model(config: Any) -> str:
    return str(config_get(config, "model_sharing.model", "gpt-4o")).strip()


def model_sharing_confirm_prompt(config: Any) -> bool:
    return bool(config_get(config, "model_sharing.confirm_prompt", True))


def model_sharing_notify(config: Any) -> bool:
    return bool(config_get(config, "model_sharing.notify", True))


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
    raw_terms = config_get(config, "model_sharing.redaction_terms", [])
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
        return model_sharing_enabled(self.config)

    def _provider(self) -> str:
        return model_sharing_provider(self.config)

    def _model(self) -> str:
        return model_sharing_model(self.config)

    async def run(self, prompt: str, *, redacted_prompt: str = "") -> str:
        if not self.is_enabled():
            return "Advanced reasoning is disabled in settings."

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return "Advanced reasoning request is empty."

        provider = self._provider()
        model = self._model()
        if provider not in ADVANCED_REASONING_PROVIDERS:
            return f"Unsupported advanced reasoning provider: {provider}"
        if not model:
            return "Advanced reasoning model is not configured."

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
            confirm=model_sharing_confirm_prompt(self.config),
            notify=model_sharing_notify(self.config),
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
        from ..llm.manager import create_llm_client

        temp_config = clone_config(self.config)
        config_set(temp_config, "llm_provider", provider)
        config_set(temp_config, "llm_model", model)
        config_set(temp_config, "use_tools", False)
        config_set(temp_config, "skills.enabled", False)
        config_set(temp_config, "memory.enabled", False)
        config_set(temp_config, f"{provider}.model", model)

        client = create_llm_client(temp_config)
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
