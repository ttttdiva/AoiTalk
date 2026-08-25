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


logger = logging.getLogger(__name__)
GLOBAL_CONFIG_KEY = "global"
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
)

VOICE_SESSION_ALLOWED_MODES_SOURCE_KEY = "allowed_modes_source"
VOICE_SESSION_ALLOWED_MODES_USER_PROVENANCE = "user"

# ``review_max_rounds`` was accidentally shipped as 120 in older config
# seeds.  Keep a small schema marker in the JSON config so the one-time repair
# is idempotent and an explicitly customized value can be distinguished from a
# value that has already passed through the repair.
APP_CONFIG_SCHEMA_VERSION = 2
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


def _migrate_legacy_autofilled_defaults(
    value: Dict[str, Any], seed: Dict[str, Any]
) -> Dict[str, Any]:
    """Replace untouched legacy auto-fill values with the current defaults."""
    migrated = copy.deepcopy(value)

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
                    APP_CONFIG_SCHEMA_VERSION,
                    _config_schema_version(seed),
                )

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

    try:
        from .services.agent_team_v2_migration import migrate_agent_team_config

        return migrate_agent_team_config(value)
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
        return result


# Compatibility name for callers that previously reached the private v2
# helper.  Both paths now produce the same canonical schema-v3 envelope.
_migrate_agent_team_v3_config = _migrate_agent_team_v2_config


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

    seed = (
        copy.deepcopy(seed_override)
        if seed_override is not None
        else (
            _load_legacy_yaml(legacy_config_path)
            if legacy_config_path is not None
            else None
        )
    ) or load_default_config()
    seed = _migrate_agent_team_v2_config(
        _migrate_writing_model_routing(
            _migrate_shared_integrations(
                _migrate_external_model_privacy(_prune_obsolete_app_config(seed))
            )
        )
    )

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
                session.commit()
                stored_value = encrypted_value

            stored_decrypted = decrypt_json_secret_leaves(
                stored_value,
                aad_prefix="app_config_settings.value",
            )
            value = _migrate_legacy_autofilled_defaults(
                _migrate_agent_team_v2_config(
                    _migrate_writing_model_routing(
                        _migrate_shared_integrations(
                            _migrate_external_model_privacy(
                                _prune_obsolete_app_config(stored_decrypted)
                            )
                        )
                    )
                ),
                seed,
            )
            merged = _fill_missing_defaults(value, seed)
            if merged != stored_decrypted:
                row.value = encrypt_json_secret_leaves(
                    copy.deepcopy(merged),
                    aad_prefix="app_config_settings.value",
                )
                session.commit()
            return copy.deepcopy(merged)
    except Exception as exc:
        logger.error("Failed to load app configuration from DB: %s", exc)
        require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
            "1", "true", "yes", "on"
        } or Features.is_enterprise()
        if require_database:
            raise RuntimeError(
                "Enterprise app configuration could not be loaded from PostgreSQL"
            ) from exc
        return copy.deepcopy(seed)


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
        config = _migrate_agent_team_v2_config(
            _migrate_writing_model_routing(
                _migrate_shared_integrations(
                    _migrate_external_model_privacy(_prune_obsolete_app_config(config))
                )
            )
        )
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
                    config = _migrate_agent_team_v2_config(
                        _migrate_writing_model_routing(
                            _migrate_shared_integrations(
                                _migrate_external_model_privacy(
                                    _prune_obsolete_app_config(load_default_config())
                                )
                            )
                        )
                    )
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
                    if current_value is missing or current_value != expected:
                        session.rollback()
                        return False

                for key, parts, value in parsed_changes:
                    current: Dict[str, Any] = config
                    for part in parts[:-1]:
                        next_value = current.get(part)
                        if not isinstance(next_value, dict):
                            next_value = {}
                            current[part] = next_value
                        current = next_value
                    current[parts[-1]] = copy.deepcopy(value)
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

