"""Database-backed application configuration storage."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config_defaults import load_default_config
from .features import Features
from .tts.irodori_config import normalize_irodori_settings
from .security.field_crypto import (
    decrypt_json_secret_leaves,
    encrypt_json_secret_leaves,
)
from .utils.logging_config import FILE_ONLY_LOG_EXTRA


logger = logging.getLogger(__name__)
GLOBAL_CONFIG_KEY = "global"
# Identity-only marker for a dotted key that must not exist during a CAS.
APP_CONFIG_MISSING = object()


class AppConfigSnapshotUnavailable(RuntimeError):
    """The read-only effective configuration snapshot could not be loaded.

    This sentinel intentionally carries no database, decryption, endpoint, or
    provider details.  Overview workers use it to fail closed instead of
    silently routing a request through stale startup configuration.
    """

    code = "config_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


OBSOLETE_AGENT_TEAM_MEMBER_KEYS = {
    "search",
    "filesystem",
    "project_management",
    "skills",
    # 高度推論は外部送信の責務と一緒に廃止され、Agent Teamのmemberではない。
    "advanced_reasoning",
}

# agents.<key>.enabled のうち、ランタイムが一切参照しなくなったトグル。
# utility / media は Agent Team メンバー判定（agent_team.members）へ移行済みで、
# _agent_enabled() が agents 配下を読まないため保存しておく意味がない。
# spotify は同じくデッドだが Web/mobile の設定UIがまだ書き込むため、
# UI撤去とセットで削除する（ここで消すとトグルがOFFを保持できなくなる）。
OBSOLETE_AGENT_TOGGLE_KEYS = {
    "skills",
    "utility",
    "media",
}

# Legacy memory settings for the removed SentenceTransformer embedding path.
# Semantic conversation search is owned by CrossSessionMemoryService/src.rag
# (EmbeddingGemma 2 + Qdrant), so these keys have no runtime consumer and must not be
# retained in the persisted application configuration.
OBSOLETE_MEMORY_CONFIG_KEYS = {
    "embedding_model",
    "preload_embedding_model",
}

_WRITING_MODEL_ROUTING_DEFAULT: Dict[str, Any] = {
    "inherit": True,
    "provider": "",
    "model": "",
    "base_url": "",
    "api_key": "",
    "reasoning_effort": "",
}


def _migrate_external_model_privacy(value: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy Agent Team privacy options to the independent policy.

    ``confirm_prompt`` intentionally does not become ``always``: that would
    unexpectedly turn every existing Personal request into a blocking modal.
    Existing terms/notify values are retained when the new branch is absent;
    explicit new settings are never overwritten.
    """

    migrated = copy.deepcopy(value)
    privacy = migrated.get("external_model_privacy")
    had_privacy = isinstance(privacy, dict)
    if not had_privacy:
        privacy = {}
        migrated["external_model_privacy"] = privacy
    agent_team = migrated.get("agent_team")
    if isinstance(agent_team, dict):
        if not had_privacy:
            legacy_terms = agent_team.get("redaction_terms")
            if isinstance(legacy_terms, list):
                privacy["redaction_terms"] = copy.deepcopy(legacy_terms)
            if "notify" in agent_team:
                privacy["notify"] = bool(agent_team.get("notify"))
        for key in ("confirm_prompt", "notify", "redaction_terms"):
            agent_team.pop(key, None)
    if "mode" not in privacy:
        try:
            privacy["mode"] = "protected" if Features.is_enterprise() else "direct"
        except Exception:  # noqa: BLE001
            privacy["mode"] = "direct"
    return migrated


def _migrate_writing_model_routing(value: Dict[str, Any]) -> Dict[str, Any]:
    """既存 DB 設定へ執筆クラスだけを一度補完する。

    既存の ``writing`` がある場合は、空値を含めてユーザー設定を変更しない。
    API キーの実値を新設することもない。
    """

    migrated = copy.deepcopy(value)
    routing = migrated.get("model_routing")
    if not isinstance(routing, dict):
        routing = {}
        migrated["model_routing"] = routing
    classes = routing.get("classes")
    if not isinstance(classes, dict):
        classes = {}
        routing["classes"] = classes
    if "writing" not in classes:
        classes["writing"] = copy.deepcopy(_WRITING_MODEL_ROUTING_DEFAULT)
    return migrated


def _load_legacy_yaml(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read legacy config YAML %s: %s", path, exc)
        return None


def _fill_missing_defaults(value: Any, defaults: Any) -> Any:
    """Return value with missing keys filled from defaults, preserving user values."""
    if not isinstance(value, dict) or not isinstance(defaults, dict):
        return copy.deepcopy(value)

    merged = copy.deepcopy(value)
    for key, default_value in defaults.items():
        # Agent Team is an explicit user-managed topology.  Once the v3
        # section has been persisted, an omitted/deleted Team or Subagent is
        # intentional and must not be recreated by the generic recursive
        # default filler.  The dedicated schema migration below owns this
        # section and supplies defaults only for legacy/nonexistent data.
        if key == "agent_team":
            continue
        if key not in merged:
            merged[key] = copy.deepcopy(default_value)
        elif isinstance(merged[key], dict) and isinstance(default_value, dict):
            merged[key] = _fill_missing_defaults(merged[key], default_value)
    return merged


def _prune_obsolete_app_config(value: Dict[str, Any]) -> Dict[str, Any]:
    """Drop stale config branches that no longer have a runtime owner."""
    cleaned = copy.deepcopy(value)
    cleaned.pop("model_sharing", None)
    # 廃止したOpenCode IP Rotator providerの設定。Main選択が残る場合は
    # 既定値へ戻し、他providerの設定には触れない。
    cleaned.pop("opencode_ip_rotator", None)
    if str(cleaned.get("llm_provider") or "").strip() == "opencode-ip-rotator":
        cleaned.pop("llm_provider", None)
        cleaned.pop("llm_model", None)

    agent_team = cleaned.get("agent_team")
    if isinstance(agent_team, dict):
        # 実行ロジックが参照しない旧オーケストレーション設定は残さない。
        agent_team.pop("strategy", None)
        agent_team.pop("spawn_policy", None)
        members = agent_team.get("members")
        if isinstance(members, dict):
            for member_key in OBSOLETE_AGENT_TEAM_MEMBER_KEYS:
                members.pop(member_key, None)
        roster = agent_team.get("roster")
        if isinstance(roster, list):
            agent_team["roster"] = [
                item
                for item in roster
                if not (
                    isinstance(item, dict)
                    and str(
                        item.get("member_key")
                        or item.get("key")
                        or item.get("id")
                        or ""
                    )
                    in OBSOLETE_AGENT_TEAM_MEMBER_KEYS
                )
            ]

    chatgpt_web = cleaned.get("chatgpt_web")
    if isinstance(chatgpt_web, dict):
        # Web版ChatGPTは表示ありでのみ操作し、旧headless設定は廃止する。
        chatgpt_web.pop("headless", None)

    agents = cleaned.get("agents")
    if isinstance(agents, dict):
        for agent_key in OBSOLETE_AGENT_TOGGLE_KEYS:
            agents.pop(agent_key, None)

    memory = cleaned.get("memory")
    if isinstance(memory, dict):
        for memory_key in OBSOLETE_MEMORY_CONFIG_KEYS:
            memory.pop(memory_key, None)

    tts_settings = cleaned.get("tts_settings")
    if isinstance(tts_settings, dict):
        for engine_key in ("irodori_tts", "miotts"):
            engine_settings = tts_settings.get(engine_key)
            if isinstance(engine_settings, dict):
                engine_settings.pop("cache_dir", None)
        irodori_settings = tts_settings.get("irodori_tts")
        if isinstance(irodori_settings, dict):
            normalize_irodori_settings(irodori_settings)

    tts = cleaned.get("tts")
    if isinstance(tts, dict):
        yomi_settings = tts.get("yomi_linter")
        if isinstance(yomi_settings, dict):
            yomi_settings.pop("cache_dir", None)

    return cleaned


def _migrate_shared_integrations(value: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy Spotify availability toggles to the Shared Integration.

    ``keyword_detection.spotify.enabled`` remains a detector preference and is
    deliberately not used as the integration availability source.  Existing
    users' explicit legacy Spotify/agent toggles are copied once; a fresh seed
    gets the disabled integration default from ``config_defaults``.
    """
    migrated = copy.deepcopy(value)
    integrations = migrated.get("integrations")
    if not isinstance(integrations, dict):
        integrations = {}
        migrated["integrations"] = integrations
    spotify_integration = integrations.get("spotify")
    if not isinstance(spotify_integration, dict):
        spotify_integration = {}
        integrations["spotify"] = spotify_integration
    if "enabled" not in spotify_integration:
        legacy_enabled: Any = None
        legacy_spotify = migrated.get("spotify")
        if isinstance(legacy_spotify, dict) and "enabled" in legacy_spotify:
            legacy_enabled = legacy_spotify.get("enabled")
        agents = migrated.get("agents")
        if legacy_enabled is None and isinstance(agents, dict):
            legacy_agent_spotify = agents.get("spotify")
            if isinstance(legacy_agent_spotify, dict) and "enabled" in legacy_agent_spotify:
                legacy_enabled = legacy_agent_spotify.get("enabled")
        # Absence is the new safe default; an explicit legacy value is copied
        # once, then the obsolete toggle is removed so the App Config has one
        # canonical integration source.  Keyword detection remains separate.
        spotify_integration["enabled"] = bool(legacy_enabled) if legacy_enabled is not None else False
    legacy_spotify = migrated.get("spotify")
    if isinstance(legacy_spotify, dict):
        legacy_spotify.pop("enabled", None)
        if not legacy_spotify:
            migrated.pop("spotify", None)
    agents = migrated.get("agents")
    if isinstance(agents, dict):
        legacy_agent_spotify = agents.get("spotify")
        if isinstance(legacy_agent_spotify, dict):
            legacy_agent_spotify.pop("enabled", None)
            if not legacy_agent_spotify:
                agents.pop("spotify", None)
    return migrated


# 旧 config_defaults が自動充填していた既定値。ユーザーが触っていない証拠なので、
# これと完全一致する場合だけ新しい既定値へ載せ替える。1文字でも違えばユーザー設定
# とみなして保持する。
_LEGACY_AUTOFILLED_DEFAULTS: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("os_operations", "protected_paths"), ["C:\\", "D:\\", "E:\\", "F:\\", "G:\\"]),
    (("agentic_completion", "max_rounds"), 2),
    (("keyword_detection", "llm_model"), "gpt-5-mini"),
    # ``realtime_character_tts`` was temporarily removed from the shipped
    # seed.  Treat the resulting native-only list as an untouched generated
    # value, while preserving an explicit user choice marked by the atomic
    # settings API below.
    (("voice_sessions", "allowed_modes"), ["realtime_native"]),
    # OpenRouter tool use shipped default-off.  A stored ``false`` without the
    # explicit-user marker is that generated value and follows the new default.
    (("openrouter", "enable_tools"), False),
    # Browser operation ran through a PC Bridge by default.  It now runs on this
    # server (local); a stored "bridge" was only the generated default, since the
    # settings API never exposed browser.backend.
    (("browser", "backend"), "bridge"),
)

# The retired compact OpenAI model identifier must not survive in the persisted
# application configuration.  Keep the legacy token assembled here rather than
# sprinkling it through the runtime; this helper is migration-only and never
# chooses the legacy model for a request.
_LEGACY_COMPACT_MODEL = "gpt-4o-" + "mini"

# Provider-specific model keys are the existing config authority.  Migration
# reads the current seed through these paths rather than inventing a second
# catalog or silently forcing every provider onto the OpenAI model.
_PROVIDER_MODEL_CONFIG_PATHS: dict[str, tuple[str, ...]] = {
    "openai": ("openai", "model"),
    "openrouter": ("openrouter", "model"),
    "gemini": ("gemini", "model"),
    "deepseek": ("deepseek", "model"),
    "deepinfra": ("deepinfra", "model"),
    "kimi": ("kimi", "model"),
    "ollama": ("ollama", "model"),
    "sglang": ("sglang", "model"),
    "openai_compatible_local": ("openai_compatible_local", "model"),
    "codex-cli": ("codex_cli", "model"),
    "claude-cli": ("claude_cli", "model"),
    "grok-cli": ("grok_cli", "model"),
    "antigravity-cli": ("antigravity_cli", "model"),
}


def _dotted_config_value(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalise_provider_id(provider: Any) -> str:
    return str(provider or "").strip().lower().replace("_", "-")


def _canonical_model_for_provider(provider: Any, seed: Dict[str, Any]) -> str:
    """Resolve a migration replacement from the provider's existing seed."""

    if not isinstance(seed, dict):
        seed = {}
    provider_id = _normalise_provider_id(provider)
    # ``openai_compatible_local`` normalises to a dashed spelling above, while
    # the seed uses the underscored config key.
    if provider_id == "openai-compatible-local":
        provider_id = "openai_compatible_local"
    path = _PROVIDER_MODEL_CONFIG_PATHS.get(provider_id)
    candidate = _dotted_config_value(seed, path) if path else None
    if candidate is None or not str(candidate).strip():
        if provider_id == "openai":
            candidate = seed.get("llm_model")
    candidate_text = str(candidate or "").strip()
    if _is_legacy_compact_model(candidate_text):
        # A caller may pass an old external seed as ``seed_override``.  Never
        # use that old value as the migration replacement.
        return ""
    return candidate_text


def _is_legacy_compact_model(value: Any) -> bool:
    text = str(value or "").strip()
    model_id = text.rsplit("/", 1)[-1].lower()
    if model_id == _LEGACY_COMPACT_MODEL:
        return True
    # Also retire dated/realtime snapshots such as ``...-2024-07-18``.  The
    # runtime resolver rejects the whole retired family, so persisted config
    # must not reintroduce one through a voice or specialist route.
    return model_id.startswith(_LEGACY_COMPACT_MODEL + "-")


def _migrate_legacy_compact_models(
    value: Dict[str, Any], seed: Dict[str, Any]
) -> Dict[str, Any]:
    """Replace retired compact-model values in persisted config.

    This walks only model-bearing keys, preserving arbitrary strings elsewhere.
    The provider on each route wins; otherwise the surrounding provider branch,
    then the top-level provider, determines the replacement.  Unknown provider
    values fail closed to an empty model instead of falling through to OpenAI.
    """

    migrated = copy.deepcopy(value)
    seed = seed if isinstance(seed, dict) else {}
    root_provider = migrated.get("llm_provider") or seed.get("llm_provider")
    model_keys = {
        "model",
        "llm_model",
        "openai_model",
        "openrouter_model",
        "gemini_model",
        "deepseek_model",
        "deepinfra_model",
        "kimi_model",
        "ollama_model",
        "sglang_model",
        "openai_compatible_local_model",
        "codex_model",
        "claude_model",
        "grok_model",
        "antigravity_cli_model",
    }
    provider_key_aliases = {
        "openai_model": "openai",
        "openrouter_model": "openrouter",
        "gemini_model": "gemini",
        "deepseek_model": "deepseek",
        "deepinfra_model": "deepinfra",
        "kimi_model": "kimi",
        "ollama_model": "ollama",
        "sglang_model": "sglang",
        "openai_compatible_local_model": "openai_compatible_local",
        "codex_model": "codex-cli",
        "claude_model": "claude-cli",
        "grok_model": "grok-cli",
        "antigravity_cli_model": "antigravity-cli",
    }

    def visit(node: Any, inherited_provider: Any, branch_name: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, inherited_provider, branch_name)
            return
        if not isinstance(node, dict):
            return

        local_provider = node.get("provider") or inherited_provider
        branch_provider = (
            branch_name
            if branch_name in _PROVIDER_MODEL_CONFIG_PATHS
            else local_provider
        )
        for key, child in list(node.items()):
            provider_for_key = provider_key_aliases.get(key, branch_provider)
            if key in model_keys and _is_legacy_compact_model(child):
                replacement = _canonical_model_for_provider(provider_for_key, seed)
                # A top-level model with no provider is an invalid route.  Use
                # the canonical OpenAI seed only when the provider is actually
                # OpenAI; all other unresolved routes remain fail-closed.
                node[key] = replacement
                logger.warning(
                    "Migrated retired compact model at %s to %s",
                    key,
                    replacement or "<empty/fail-closed>",
                )
                continue
            child_branch = key if key in _PROVIDER_MODEL_CONFIG_PATHS else branch_name
            visit(child, provider_for_key, child_branch)

    visit(migrated, root_provider)
    return migrated

VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY = "allowed_modes_source"
VOICE_SESSION_ALLOWED_MODES_USER_PROVENANCE = "user"
OPENROUTER_ENABLE_TOOLS_KEY = "openrouter.enable_tools"
OPENROUTER_ENABLE_TOOLS_SOURCE_KEY = "enable_tools_source"
OPENROUTER_ENABLE_TOOLS_SOURCE_PATH = "openrouter.enable_tools_source"
OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE = "user"

# ``review_max_rounds`` was accidentally shipped as 120 in older config
# seeds.  Keep a small schema marker in the JSON config so the one-time repair
# is idempotent and an explicitly customized value can be distinguished from a
# value that has already passed through the repair.
APP_CONFIG_SCHEMA_VERSION = 3
AGENT_RUNTIME_REMOVAL_MIGRATION_ID = "agent_runtime_v2_removal"
AGENT_RUNTIME_REMOVAL_SCHEMA_VERSION = 3
LEGACY_REVIEW_MAX_ROUNDS_DEFAULT = 120
REVIEW_MAX_ROUNDS_SCHEMA_VERSION = 2
LEGACY_AGENTIC_COMPLETION_DEFAULTS: Dict[str, Any] = {
    "max_rounds": 12,
    "max_tool_rounds": 24,
    "managed_workspace_max_rounds": 2,
    "work_max_rounds": 120,
    "assisted_work_max_rounds": 120,
    "autonomous_work_max_rounds": 120,
    "review_max_rounds": LEGACY_REVIEW_MAX_ROUNDS_DEFAULT,
    "project_progress_max_rounds": 120,
}
REVIEW_MAX_ROUNDS_PROVENANCE_KEY = "review_max_rounds_source"
REVIEW_MAX_ROUNDS_USER_PROVENANCE = "user"
REVIEW_MAX_ROUNDS_CUSTOM_PROVENANCE = frozenset(
    {"user", "custom", "explicit", "operator"}
)


def _config_schema_version(value: Any) -> int:
    """Return a positive app-config schema version, or zero when absent."""

    if not isinstance(value, dict):
        return 0
    try:
        parsed = int(value.get("app_config_schema_version", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _migrate_retired_agent_runtime_config(value: Dict[str, Any]) -> Dict[str, Any]:
    """Archive and remove the retired Team/Profile configuration branches.

    The archive is made from the decrypted value and is encrypted again by the
    caller together with the rest of ``app_config_settings``.  Keeping the
    snapshot inside the same JSON document means the existing field encryption
    and API secret masking continue to apply to historical values as well.

    This is intentionally independent from the old Agent Team schema migration:
    the latter must never be allowed to recreate a Team after this marker has
    been written.  A stable migration id makes the operation safe to repeat if
    a process is restarted after reading an already migrated row.
    """

    migrated = copy.deepcopy(value)
    if not isinstance(migrated, dict):
        migrated = {}

    legacy_team = migrated.get("agent_team")
    legacy_profiles = migrated.get("execution_profiles")
    routing_profiles = migrated.get("routing_profiles")
    free_team = (
        routing_profiles.get("free-team")
        if isinstance(routing_profiles, dict)
        else None
    )
    legacy_free_team_profiles = (
        free_team.get("llm_profiles") if isinstance(free_team, dict) else None
    )
    has_legacy_branch = (
        "agent_team" in migrated
        or "execution_profiles" in migrated
        or isinstance(free_team, dict) and "llm_profiles" in free_team
    )

    snapshots = migrated.get("legacy_snapshots")
    if has_legacy_branch and not isinstance(snapshots, dict):
        snapshots = {}
        migrated["legacy_snapshots"] = snapshots
    if has_legacy_branch and AGENT_RUNTIME_REMOVAL_MIGRATION_ID not in snapshots:
        snapshot: dict[str, Any] = {}
        if "agent_team" in migrated:
            snapshot["agent_team"] = copy.deepcopy(legacy_team)
        if "execution_profiles" in migrated:
            snapshot["execution_profiles"] = copy.deepcopy(legacy_profiles)
        if isinstance(free_team, dict) and "llm_profiles" in free_team:
            snapshot["routing_profiles"] = {
                "free-team": {
                    "llm_profiles": copy.deepcopy(legacy_free_team_profiles),
                }
            }
        snapshots[AGENT_RUNTIME_REMOVAL_MIGRATION_ID] = snapshot

    # Preserve the old privacy migration before dropping the Team envelope.
    # Its values are included in the archive above and its independent policy
    # branch remains available to current runtime code.
    if isinstance(legacy_team, dict):
        migrated = _migrate_external_model_privacy(migrated)
    migrated.pop("agent_team", None)
    migrated.pop("execution_profiles", None)
    if isinstance(routing_profiles, dict):
        current_routing = migrated.get("routing_profiles")
        current_free_team = (
            current_routing.get("free-team")
            if isinstance(current_routing, dict)
            else None
        )
        if isinstance(current_free_team, dict):
            current_free_team.pop("llm_profiles", None)

    # A schema marker is useful even when a particular old branch was absent:
    # it prevents a later seed/default merge from reintroducing the removed
    # branch during the same startup sequence.
    migrated["app_config_schema_version"] = max(
        AGENT_RUNTIME_REMOVAL_SCHEMA_VERSION,
        _config_schema_version(migrated),
    )
    return migrated


def _migrate_legacy_autofilled_defaults(
    value: Dict[str, Any], seed: Dict[str, Any]
) -> Dict[str, Any]:
    """Replace untouched legacy auto-fill values with the current defaults."""
    migrated = _migrate_legacy_compact_models(value, seed)

    for path, legacy_value in _LEGACY_AUTOFILLED_DEFAULTS:
        stored_parent: Any = migrated
        seed_parent: Any = seed
        for key in path[:-1]:
            if not isinstance(stored_parent, dict) or not isinstance(seed_parent, dict):
                stored_parent = None
                break
            stored_parent = stored_parent.get(key)
            seed_parent = seed_parent.get(key)
        leaf = path[-1]
        if not isinstance(stored_parent, dict) or not isinstance(seed_parent, dict):
            continue
        if leaf not in stored_parent or leaf not in seed_parent:
            continue
        if stored_parent[leaf] != legacy_value:
            continue
        if seed_parent[leaf] == legacy_value:
            continue
        if path == ("voice_sessions", "allowed_modes"):
            source = str(
                stored_parent.get(VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY) or ""
            ).strip().lower()
            if source == VOICE_SESSION_ALLOWED_MODES_USER_PROVENANCE or bool(
                stored_parent.get("allowed_modes_customized")
            ):
                continue
        if path == ("openrouter", "enable_tools"):
            source = str(
                stored_parent.get(OPENROUTER_ENABLE_TOOLS_SOURCE_KEY) or ""
            ).strip().lower()
            if source == OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE:
                continue
        stored_parent[leaf] = copy.deepcopy(seed_parent[leaf])
        logger.info(
            "Migrated untouched legacy default %s to the current value",
            ".".join(path),
        )

    # Older databases can contain the accidental review default of 120.  The
    # unversioned form is migrated only when the whole known legacy
    # ``agentic_completion`` seed shape is still present.  This avoids treating
    # an arbitrary user payload as a generated default.  A caller that has
    # intentionally chosen 120 can preserve it in an unversioned payload with
    # ``review_max_rounds_source=user``; the update path writes this provenance
    # marker for future changes.  Once marked, the migration is idempotent.
    stored_schema_version = _config_schema_version(value)
    if stored_schema_version < REVIEW_MAX_ROUNDS_SCHEMA_VERSION:
        stored_agentic = migrated.get("agentic_completion")
        seed_agentic = seed.get("agentic_completion")
        if isinstance(stored_agentic, dict) and isinstance(seed_agentic, dict):
            stored_review = stored_agentic.get("review_max_rounds")
            seed_review = seed_agentic.get("review_max_rounds")
            provenance = str(
                stored_agentic.get(REVIEW_MAX_ROUNDS_PROVENANCE_KEY) or ""
            ).strip().lower()
            explicit_custom = provenance in REVIEW_MAX_ROUNDS_CUSTOM_PROVENANCE or bool(
                stored_agentic.get("review_max_rounds_customized")
            )
            legacy_shape_matches = all(
                stored_agentic.get(key) == expected
                for key, expected in LEGACY_AGENTIC_COMPLETION_DEFAULTS.items()
            )
            if (
                stored_review == LEGACY_REVIEW_MAX_ROUNDS_DEFAULT
                and seed_review != LEGACY_REVIEW_MAX_ROUNDS_DEFAULT
                and not explicit_custom
                and legacy_shape_matches
            ):
                stored_agentic["review_max_rounds"] = copy.deepcopy(seed_review)
                logger.info(
                    "Migrated untouched legacy default agentic_completion.review_max_rounds "
                    "from %s to %s",
                    LEGACY_REVIEW_MAX_ROUNDS_DEFAULT,
                    seed_review,
                )

            # Keep the marker alongside an existing agentic config so this
            # migration is idempotent even when callers invoke the helper
            # directly instead of going through ``_fill_missing_defaults``.
            if "review_max_rounds" in stored_agentic:
                migrated["app_config_schema_version"] = max(
                    REVIEW_MAX_ROUNDS_SCHEMA_VERSION,
                    _config_schema_version(value),
                )

    return migrated


_LEGACY_EMPLOYEE_OPERATOR_DESCRIPTION = "管理者が設定した自動化と電話受付を担当する。"
_LEGACY_EMPLOYEE_OPERATOR_INSTRUCTIONS = (
    "承認された範囲の業務のみ扱う。外部操作は登録済みActionPolicyで検証し、"
    "電話転送には許可されたdestination_keyだけを使う。"
)


def _migrate_employee_operator_telephony_text(
    value: Dict[str, Any],
) -> Dict[str, Any]:
    """Replace only the exact retired builtin Employee Operator text."""

    migrated = copy.deepcopy(value)
    agent_team = migrated.get("agent_team")
    subagents = agent_team.get("subagents") if isinstance(agent_team, dict) else None
    employee_operator = (
        subagents.get("employee_operator")
        if isinstance(subagents, dict)
        else None
    )
    if not isinstance(employee_operator, dict):
        return migrated

    try:
        from .services.agent_team_v3 import AGENT_TEAM_SUBAGENT_CATALOG

        current_builtin = AGENT_TEAM_SUBAGENT_CATALOG["employee_operator"]
        if employee_operator.get("description") == _LEGACY_EMPLOYEE_OPERATOR_DESCRIPTION:
            employee_operator["description"] = current_builtin["description"]
        if employee_operator.get("instructions") == _LEGACY_EMPLOYEE_OPERATOR_INSTRUCTIONS:
            employee_operator["instructions"] = current_builtin["instructions"]
    except (ImportError, KeyError):
        # Keep configuration loading available during partial bootstrap.
        logger.debug("Employee Operator text migration deferred", exc_info=True)
    return migrated


def _migrate_agent_team_v2_config(value: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate Agent Team input to the canonical schema-v3 shape.

    This is deliberately a post-privacy migration. ``external_model_privacy``
    remains the only source for outbound policy and the background
    ``agent_harness`` settings stay in their independent top-level branch.
    v2 Template/Member/Model Group containers are consumed as migration input
    only; the value returned to callers contains exactly the v3 canonical
    fields.  Once a v3 config has been persisted, this migration never fills a
    missing Team/Subagent/Profile (so user deletions remain deleted).
    """

    # P5 archives and removes the Team/Profile envelope.  From that schema
    # onward this compatibility helper must be a no-op; invoking the old
    # converter here would seed a fresh Team on every startup.
    if _config_schema_version(value) >= AGENT_RUNTIME_REMOVAL_SCHEMA_VERSION:
        return copy.deepcopy(value)

    try:
        from .services.agent_team_v2_migration import migrate_agent_team_config

        return _migrate_employee_operator_telephony_text(
            migrate_agent_team_config(value)
        )
    except Exception:
        # Config loading must remain available even when an optional service
        # import is unavailable during bootstrap.  Keep a minimal canonical
        # envelope rather than returning a v2 object to the persistence layer.
        logger.debug("Agent Team v3 migration deferred", exc_info=True)
        result = copy.deepcopy(value)
        raw = result.get("agent_team") if isinstance(result.get("agent_team"), dict) else {}
        result["agent_team"] = {
            "schema_version": 3,
            "delegation_enabled": bool(raw.get("delegation_enabled", False)),
            "orchestration_mode": "director" if raw.get("orchestration_mode") == "director" else "standard",
            "teams": copy.deepcopy(raw.get("teams")) if isinstance(raw.get("teams"), dict) else {},
            "subagents": copy.deepcopy(raw.get("subagents")) if isinstance(raw.get("subagents"), dict) else {},
        }
        return _migrate_employee_operator_telephony_text(result)


# Compatibility name for callers that previously reached the private v2
# helper.  Both paths now produce the same canonical schema-v3 envelope.
_migrate_agent_team_v3_config = _migrate_agent_team_v2_config


def _migrate_app_config_value(
    value: Dict[str, Any], *, seed: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """Apply app-config migrations in their ownership order.

    The P5 archive must run before obsolete-branch pruning and before the old
    Team converter.  Otherwise either the historical snapshot would lose
    fields or the converter could recreate a Team from a missing branch.
    """

    migrated = _migrate_retired_agent_runtime_config(value)
    migrated = _migrate_external_model_privacy(migrated)
    migrated = _prune_obsolete_app_config(migrated)
    migrated = _migrate_shared_integrations(migrated)
    migrated = _migrate_writing_model_routing(migrated)
    migrated = _migrate_agent_team_v2_config(migrated)
    if seed is not None:
        migrated = _migrate_legacy_autofilled_defaults(migrated, seed)
    return migrated


def _db_deps():
    from sqlalchemy.exc import SQLAlchemyError

    from .memory.database import get_database_manager
    from .memory.models import AppConfigSetting

    return SQLAlchemyError, get_database_manager, AppConfigSetting


def _ensure_table() -> None:
    _, get_database_manager, AppConfigSetting = _db_deps()
    db_manager = get_database_manager()
    if not db_manager.is_initialized():
        raise RuntimeError(
            "Database migrations must complete before accessing app_config_settings"
        )


def load_app_config_sync(
    legacy_config_path: Optional[Path] = None,
    *,
    seed_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Load the global app config from DB, seeding it on first use."""

    canonical_seed = load_default_config()
    seed = (
        copy.deepcopy(seed_override)
        if seed_override is not None
        else (
            _load_legacy_yaml(legacy_config_path)
            if legacy_config_path is not None
            else None
        )
    ) or copy.deepcopy(canonical_seed)
    seed = _migrate_app_config_value(seed)
    # Legacy YAML/seed overrides can carry the retired compact model too.  Do
    # this before a first-use DB insert so a fresh row is never seeded with it.
    seed = _migrate_legacy_compact_models(seed, canonical_seed)

    try:
        SQLAlchemyError, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
            if row is None:
                row = AppConfigSetting(
                    key=GLOBAL_CONFIG_KEY,
                    value=encrypt_json_secret_leaves(
                        copy.deepcopy(seed),
                        aad_prefix="app_config_settings.value",
                    ),
                )
                session.add(row)
                session.commit()
                logger.info("Seeded app configuration into database")
                return copy.deepcopy(seed)
            stored_value = row.value if isinstance(row.value, dict) else {}
            encrypted_value = encrypt_json_secret_leaves(
                stored_value,
                aad_prefix="app_config_settings.value",
            )
            if encrypted_value != stored_value:
                row.value = copy.deepcopy(encrypted_value)
                stored_value = encrypted_value

            stored_decrypted = decrypt_json_secret_leaves(
                stored_value,
                aad_prefix="app_config_settings.value",
            )
            value = _migrate_app_config_value(stored_decrypted, seed=seed)
            merged = _fill_missing_defaults(value, seed)
            if merged != stored_decrypted:
                row.value = encrypt_json_secret_leaves(
                    copy.deepcopy(merged),
                    aad_prefix="app_config_settings.value",
                )
                session.commit()
            return copy.deepcopy(merged)
    except Exception as exc:
        # Personal installs intentionally fall back to the seed config when
        # the optional settings database is unavailable. Retain the exact
        # exception in the application log without turning this expected
        # fallback into an operator-facing error flood; the caller emits a
        # concise actionable warning/error when the database is required.
        logger.error(
            "Failed to load app configuration from DB: %s",
            exc,
            extra=FILE_ONLY_LOG_EXTRA,
        )
        require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
            "1", "true", "yes", "on"
        } or Features.is_enterprise()
        if require_database:
            raise RuntimeError(
                "Enterprise app configuration could not be loaded from PostgreSQL"
            ) from exc
        return copy.deepcopy(seed)


def load_app_config_snapshot_sync(
    *,
    bootstrap_config: Any | None = None,
    legacy_config_path: Optional[Path] = None,
    seed_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Read the effective global config without migration/default writeback.

    Long-lived workers must observe DB-backed settings changed after process
    startup, but a job-time read must not seed rows, migrate encrypted values,
    or persist defaults. The optional bootstrap config supplies deployment
    environment overrides and the seed used only when a healthy DB has no
    global row; persisted DB values remain authoritative for keys that are
    present. Database, schema, decryption, and environment-merge failures
    raise :class:`AppConfigSnapshotUnavailable` so callers cannot silently
    route through stale startup configuration.
    """

    bootstrap_value = getattr(bootstrap_config, "config", bootstrap_config)
    fallback = (
        copy.deepcopy(bootstrap_value)
        if isinstance(bootstrap_value, dict)
        else load_default_config()
    )
    canonical_seed = load_default_config()
    seed = (
        copy.deepcopy(seed_override)
        if seed_override is not None
        else (
            _load_legacy_yaml(legacy_config_path)
            if legacy_config_path is not None
            else fallback
        )
    ) or copy.deepcopy(canonical_seed)
    seed = _migrate_app_config_value(seed)
    seed = _migrate_legacy_compact_models(seed, canonical_seed)

    try:
        _, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
            if row is None:
                result = copy.deepcopy(seed)
            else:
                stored_value = row.value if isinstance(row.value, dict) else {}
                decrypted = decrypt_json_secret_leaves(
                    stored_value,
                    aad_prefix="app_config_settings.value",
                )
                value = _migrate_app_config_value(decrypted)
                result = _fill_missing_defaults(value, seed)

        # Match Config's deployment-environment credential/path precedence
        # without constructing Config (which would recursively read/write DB).
        try:
            from .config import Config

            env_merger = Config.__new__(Config)
            env_merger._merge_env_variables(result)
        except Exception as exc:
            logger.warning(
                "App config snapshot environment merge unavailable: exception_type=%s",
                type(exc).__name__,
            )
            raise AppConfigSnapshotUnavailable() from exc
        return copy.deepcopy(result)
    except Exception as exc:
        logger.warning(
            "Failed to read app configuration snapshot: exception_type=%s",
            type(exc).__name__,
        )
        if isinstance(exc, AppConfigSnapshotUnavailable):
            raise
        raise AppConfigSnapshotUnavailable() from exc


def save_app_config_sync(config: Dict[str, Any]) -> bool:
    """Replace the global app config JSON in DB."""

    try:
        # A full-config save is an explicit operator action.  Record that
        # provenance for the one value whose old generated seed is being
        # migrated, so an intentional native-only choice is not widened on a
        # subsequent load.
        voice_sessions = config.get("voice_sessions")
        if isinstance(voice_sessions, dict):
            modes = voice_sessions.get("allowed_modes")
            if (
                modes == ["realtime_native"]
                and VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY not in voice_sessions
            ):
                voice_sessions = copy.deepcopy(voice_sessions)
                voice_sessions[VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY] = (
                    VOICE_SESSION_ALLOWED_MODES_USER_PROVENANCE
                )
                config = dict(config)
                config["voice_sessions"] = voice_sessions
        openrouter = config.get("openrouter")
        if (
            isinstance(openrouter, dict)
            and openrouter.get("enable_tools") is False
            and OPENROUTER_ENABLE_TOOLS_SOURCE_KEY not in openrouter
        ):
            openrouter = copy.deepcopy(openrouter)
            openrouter[OPENROUTER_ENABLE_TOOLS_SOURCE_KEY] = (
                OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE
            )
            config = dict(config)
            config["openrouter"] = openrouter
        config = _migrate_app_config_value(config)
        config = _migrate_legacy_compact_models(config, load_default_config())
        SQLAlchemyError, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
            if row is None:
                row = AppConfigSetting(
                    key=GLOBAL_CONFIG_KEY,
                    value=encrypt_json_secret_leaves(
                        copy.deepcopy(config),
                        aad_prefix="app_config_settings.value",
                    ),
                )
                session.add(row)
            else:
                row.value = encrypt_json_secret_leaves(
                    copy.deepcopy(config),
                    aad_prefix="app_config_settings.value",
                )
            session.commit()
        return True
    except Exception as exc:
        logger.error("Failed to save app configuration to DB: %s", exc)
        return False


def load_stored_app_config_sync() -> Dict[str, Any]:
    """Read the stored logical configuration for a subsequent CAS update.

    Unlike the runtime snapshot, this does not merge defaults or environment
    overrides, so its values match the row compared by update_app_config_keys_sync.
    """
    _, get_database_manager, AppConfigSetting = _db_deps()
    _ensure_table()
    with get_database_manager().get_sync_session() as session:
        row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
        if row is None or not isinstance(row.value, dict):
            return {}
        value = decrypt_json_secret_leaves(
            copy.deepcopy(row.value), aad_prefix="app_config_settings.value"
        )
        return value if isinstance(value, dict) else {}


def update_app_config_key_sync(key: str, value: Any) -> bool:
    """Update one dotted config key in the stored DB JSON."""

    return update_app_config_keys_sync({key: value})


def update_app_config_keys_sync(
    changes: dict[str, Any],
    *,
    expected_values: Optional[dict[str, Any]] = None,
) -> bool:
    """Atomically patch dotted config keys in the current global DB row.

    The global row is read with a row lock and only the requested dotted keys
    are changed, so an unrelated update committed before the lock is acquired
    is not overwritten by an older full-config snapshot.  When
    ``expected_values`` is supplied, every dotted key must still equal its
    expected value under the same lock or no change is committed.
    APP_CONFIG_MISSING requires the key to be absent; None still requires
    a present JSON null.
    """

    if not isinstance(changes, dict):
        logger.error("App configuration changes must be a dictionary")
        return False
    if expected_values is not None and not isinstance(expected_values, dict):
        logger.error("Expected app configuration values must be a dictionary")
        return False
    if not changes:
        return True

    def parse_dotted_values(
        values: dict[str, Any], *, label: str
    ) -> Optional[list[tuple[str, list[str], Any]]]:
        parsed: list[tuple[str, list[str], Any]] = []
        for key, value in values.items():
            if not isinstance(key, str):
                logger.error("%s app configuration key must be a string: %r", label, key)
                return None
            parts = key.split(".")
            if not key or any(not part for part in parts):
                logger.error("Invalid %s dotted app configuration key: %r", label, key)
                return None
            parsed.append((key, parts, value))
        return parsed

    parsed_changes = parse_dotted_values(changes, label="changed")
    parsed_expected = parse_dotted_values(expected_values or {}, label="expected")
    if parsed_changes is None or parsed_expected is None:
        return False

    try:
        from sqlalchemy import select

        _, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            try:
                statement = (
                    select(AppConfigSetting)
                    .where(AppConfigSetting.key == GLOBAL_CONFIG_KEY)
                    .with_for_update()
                )
                row = session.execute(statement).scalar_one_or_none()
                if row is None:
                    config = _migrate_app_config_value(load_default_config())
                else:
                    stored_value = row.value if isinstance(row.value, dict) else {}
                    config = decrypt_json_secret_leaves(
                        copy.deepcopy(stored_value),
                        aad_prefix="app_config_settings.value",
                    )
                    if not isinstance(config, dict):
                        config = {}

                missing = object()
                for _key, parts, expected in parsed_expected:
                    current_value: Any = config
                    for part in parts:
                        if not isinstance(current_value, dict) or part not in current_value:
                            current_value = missing
                            break
                        current_value = current_value[part]
                    if (current_value is not missing if expected is APP_CONFIG_MISSING
                            else current_value is missing or current_value != expected):
                        session.rollback()
                        return False

                before_patch = copy.deepcopy(config)
                for key, parts, value in parsed_changes:
                    current: Dict[str, Any] = config
                    for part in parts[:-1]:
                        next_value = current.get(part)
                        if not isinstance(next_value, dict):
                            next_value = {}
                            current[part] = next_value
                        current = next_value
                    current[parts[-1]] = copy.deepcopy(value)
                    if key == OPENROUTER_ENABLE_TOOLS_KEY:
                        # Same transaction as the value: never half-applied.
                        current[OPENROUTER_ENABLE_TOOLS_SOURCE_KEY] = (
                            OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE
                        )
                    if key == "voice_sessions.allowed_modes":
                        current[VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY] = (
                            VOICE_SESSION_ALLOWED_MODES_USER_PROVENANCE
                        )
                    if key == "agentic_completion.review_max_rounds":
                        # Preserve the single-key API's explicit-user marker.
                        current[REVIEW_MAX_ROUNDS_PROVENANCE_KEY] = (
                            REVIEW_MAX_ROUNDS_USER_PROVENANCE
                        )
                        config["app_config_schema_version"] = APP_CONFIG_SCHEMA_VERSION

                from .services.openai_confirmation_service import invalidate_config_patch
                invalidate_config_patch(before_patch, config)

                # A patch is an explicit operator action, but the retired
                # compact model is no longer accepted even when supplied by a
                # stale client.  Normalize after applying the whole patch so a
                # simultaneous provider+model update resolves consistently.
                # Preserve the narrow patch semantics for an existing row:
                # archive the retired branches and prune obsolete keys, but do
                # not backfill every current seed default into an unrelated
                # operator patch.
                config = _migrate_retired_agent_runtime_config(config)
                config = _prune_obsolete_app_config(config)
                config = _migrate_legacy_compact_models(config, load_default_config())
                encrypted = encrypt_json_secret_leaves(
                    copy.deepcopy(config),
                    aad_prefix="app_config_settings.value",
                )
                if row is None:
                    row = AppConfigSetting(key=GLOBAL_CONFIG_KEY, value=encrypted)
                    session.add(row)
                else:
                    row.value = encrypted
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True
    except Exception as exc:
        logger.error(
            "Failed to patch app configuration in DB: exception_type=%s",
            type(exc).__name__,
        )
        return False
