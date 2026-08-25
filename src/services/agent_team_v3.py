"""Canonical Agent Team schema v3 helpers.

This module owns the canonical Team/Subagent graph, Team-scoped Execution
Profiles, activation resolver, capability filter, and route helpers.  Legacy
App Config conversion is isolated in the dedicated migration module.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

AGENT_TEAM_SCHEMA_VERSION = 3

# Context tags are intentionally a small, public vocabulary.  Team IDs remain
# user-owned stable identifiers and must never be used as a context/security
# boundary.  Integrations may add tags in the future without changing this
# topology (TRPG currently has only an explicit resolver hook).
AGENT_TEAM_CONTEXT_TAGS = frozenset({"app_development", "story", "trpg"})

AGENT_TEAM_CAPABILITY_CATALOG: dict[str, dict[str, Any]] = {
    "workspace_read": {"id": "workspace_read", "family": "workspace", "access": "read", "native": True},
    "workspace_write": {"id": "workspace_write", "family": "workspace", "access": "write", "native": True},
    "repo_map": {"id": "repo_map", "family": "workspace", "access": "read", "native": True},
    "command_execute": {"id": "command_execute", "family": "workspace", "access": "write", "native": True},
    "aoi_tools": {"id": "aoi_tools", "family": "aoi", "access": "read", "native": False},
    "docs_read": {"id": "docs_read", "family": "docs", "access": "read", "native": False},
    "docs_write": {"id": "docs_write", "family": "docs", "access": "write", "native": False},
    "project_read": {"id": "project_read", "family": "project", "access": "read", "native": False},
    "project_write": {"id": "project_write", "family": "project", "access": "write", "native": False},
    "web_read": {"id": "web_read", "family": "web", "access": "read", "native": False, "shared": True},
    # QA Browser is a parent-issued capability.  It is intentionally not part
    # of the default Team roster; a parent controller must inject a scoped
    # capability facade for a configured UI-QA worker.
    "browser_qa": {"id": "browser_qa", "family": "browser", "access": "write", "native": False, "parent_owned": True},
    "story_write": {"id": "story_write", "family": "story", "access": "write", "native": False},
    "story_read": {"id": "story_read", "family": "story", "access": "read", "native": False},
    "story_import": {"id": "story_import", "family": "story", "access": "write", "native": False},
    # Shared integrations are intentionally not fresh Team entries.
    "media": {"id": "media", "family": "media", "access": "write", "native": False},
    "spotify": {"id": "spotify", "family": "spotify", "access": "write", "native": False},
}
AGENT_TEAM_SHARED_READ_CAPABILITIES = frozenset({"web_read"})

PROJECT_OPERATOR_TASK_HIERARCHY_INSTRUCTIONS = (
    "タスクを新規作成する前に対象Projectの既存タスクをsearch_task_candidates（必要ならsearch付き）で確認し、"
    "詳細が必要な候補だけget_taskで開く。parent_task_id階層を尊重する。明確な既存root/containerが同じ成果を包含する場合はそのsubtaskにし、"
    "適切なrootがなければ同一目的を1つのrootと実行可能なsubtasksにまとめる。独立成果だけを別rootにする。"
    "タイトルの曖昧な類似だけで統合せず、横断的な関連や依存をparent/child containmentと混同せず、"
    "重複containerを作らない。Project更新はmanaged AoiTalk toolsで行い、filesystem/native writeは使わない。"
)

GENERAL_RESEARCHER_DELEGATION_INSTRUCTIONS = (
    "DocsやProject/Tasksの詳細は docs_operator / project_operator への委譲を前提とし、"
    "自分で high-level Docs/Project tools を直接使わない。"
)


def _subagent(
    subagent_id: str,
    name: str,
    description: str,
    instructions: str,
    capabilities: list[str],
    *,
    scalable: bool,
    default_instances: int,
    max_instances: int,
    workspace: str,
    native_tools: bool,
) -> dict[str, Any]:
    return {
        "subagent_id": subagent_id,
        "name": name,
        "description": description,
        "instructions": instructions,
        "enabled": True,
        "capability_ids": list(capabilities),
        "scalable": scalable,
        "default_instances": default_instances,
        "max_instances": max_instances,
        "max_workspace_access": workspace,
        "allow_cli_native_tools": native_tools,
    }


# Code-level catalog only; it is never serialized as App Config topology.
AGENT_TEAM_SUBAGENT_CATALOG: dict[str, dict[str, Any]] = {
    "general_worker": _subagent("general_worker", "汎用作業", "特定分野へ固定しない一般作業。比較、整理、要約、補助調査、小規模なファイル作業等。", "特定分野に固定せず、比較、整理、要約、補助調査、小規模なファイル作業を行う。CLI Agentを選択した場合はnative toolsを利用できる。", ["workspace_read", "workspace_write", "repo_map", "aoi_tools"], scalable=True, default_instances=1, max_instances=4, workspace="write", native_tools=True),
    "general_researcher": _subagent("general_researcher", "汎用調査", "Web・Workspace を直接調査し、Docs/Project は専用 operator へ委譲する横断調査。", "Web検索とWorkspace/repo mapのread-only調査を行う。DocsやProject/Tasksの詳細は docs_operator / project_operator への委譲を前提とし、自分で high-level Docs/Project tools を直接使わない。根拠を添えて報告する。", ["workspace_read", "repo_map", "web_read", "aoi_tools"], scalable=True, default_instances=1, max_instances=6, workspace="read", native_tools=True),
    "docs_operator": _subagent("docs_operator", "Docs操作", "Docsノードの検索、読み取り、整理、再構成、更新。", "AoiTalkのDocs high-level toolsのみ使用する。canonical nodeを確認し、曖昧な対象を推測して更新せず、書き込み前に対象を確認する。AoiTalk DBへ直接アクセスしない。", ["docs_read", "docs_write"], scalable=True, default_instances=1, max_instances=4, workspace="none", native_tools=False),
    "project_operator": _subagent("project_operator", "案件・タスク操作", "Projects、Tasks、Calendar、WBS、Record Tables、課題・案件情報などProject管理系AoiTalk dataを扱う。", f"Projects、Tasks、Calendar、WBS、Record Tables、課題管理、案件情報等をAoiTalk high-level tools経由で確認・更新する。{PROJECT_OPERATOR_TASK_HIERARCHY_INSTRUCTIONS}", ["project_read", "project_write", "aoi_tools"], scalable=True, default_instances=1, max_instances=4, workspace="read", native_tools=False),
    "workspace_operator": _subagent("workspace_operator", "Workspace操作", "Workspaces、ファイル認識・検索・読込・操作・整理・必要な変更を行う。", "割り当てられたWorkspaceのファイル検索、読込、複数ファイル操作、整理、必要な変更を行う。CLI providerではnative filesystem/search/edit/shell等を利用できる。", ["workspace_read", "workspace_write", "repo_map", "command_execute", "aoi_tools"], scalable=True, default_instances=1, max_instances=4, workspace="write", native_tools=True),
    "code_explorer": _subagent("code_explorer", "コード調査", "コードベース、依存関係、データフロー、既存実装等を調査するread-only Agent。", "コードベース、依存関係、データフロー、既存実装を調査し、ファイルを変更しない。", ["workspace_read", "repo_map"], scalable=True, default_instances=1, max_instances=6, workspace="read", native_tools=True),
    "architecture_planner": _subagent("architecture_planner", "設計", "実装方針、責務境界、影響範囲等を整理するread-only Agent。", "実装方針、責務境界、影響範囲等を整理し、実装計画を提案する。", ["workspace_read", "repo_map"], scalable=True, default_instances=1, max_instances=4, workspace="read", native_tools=True),
    "code_implementer": _subagent("code_implementer", "実装", "実際にWorkspaceで変更を行うAgent。", "割り当てられた変更をWorkspace sandboxで実装する。API providerではAoiTalk Workspace mutation tools、CLI providerではCLI native filesystem/search/shell/edit/test/build等を利用する。", ["workspace_read", "workspace_write", "repo_map", "command_execute", "aoi_tools"], scalable=True, default_instances=1, max_instances=4, workspace="write", native_tools=True),
    "code_reviewer": _subagent("code_reviewer", "コードレビュー", "diff、コード、関連状態をread-onlyでレビューする。", "diff、コード、関連状態をread-onlyでレビューし、実行可能な指摘だけを報告する。存在することと毎回自動起動することは別である。", ["workspace_read", "repo_map"], scalable=True, default_instances=1, max_instances=4, workspace="read", native_tools=True),
    # UI-QA is opt-in rather than part of the normal App Development roster.
    # The parent controller must inject a QABrowserCapability before this leaf
    # can receive the browser-facing ToolDefinitions.
    "ui_qa_worker": _subagent("ui_qa_worker", "UI QA", "許可されたAoiTalk originだけを検証するUI-QA worker。", "親から発行されたQA capabilityだけで対象UIを操作し、画面状態・console・network・保存/再読込結果を報告する。ChatGPT/Directorには接続しない。", ["workspace_read", "browser_qa"], scalable=False, default_instances=1, max_instances=1, workspace="read", native_tools=False),
    "story_writer": _subagent("story_writer", "執筆", "Story contextを読み、本文や設定資料を作成・更新する。", "Story contextを読み、本文や設定資料を作成・更新する。", ["story_read", "story_write"], scalable=False, default_instances=1, max_instances=1, workspace="none", native_tools=False),
    "story_consistency_reviewer": _subagent("story_consistency_reviewer", "設定整合性レビュー", "世界設定、時系列、キャラクター設定、過去シーン、用語、既存Story情報の整合性を確認するread-only Agent。", "世界設定、時系列、キャラクター設定、過去シーン、用語、既存Story情報をread-onlyで確認し、矛盾を報告する。", ["story_read"], scalable=True, default_instances=1, max_instances=4, workspace="none", native_tools=False),
    "character_voice_reviewer": _subagent("character_voice_reviewer", "キャラクター・口調レビュー", "キャラクターの人格、性格、口調、設定、既存発言との整合性を確認するread-only Agent。", "キャラクターの人格、性格、口調、設定、既存発言との整合性をread-onlyで確認し、逸脱を報告する。", ["story_read"], scalable=True, default_instances=1, max_instances=4, workspace="none", native_tools=False),
    "story_import": _subagent("story_import", "Story取り込み", "既存のStory素材取り込み機能を担当する。", "既存のStory素材を取り込み、無関係な変更を避けて正規化する。", ["story_read", "story_import"], scalable=False, default_instances=1, max_instances=1, workspace="none", native_tools=False),
}

AGENT_TEAM_DEFAULT_TEAMS: dict[str, dict[str, Any]] = {
    "general": {"team_id": "general", "name": "General", "description": "AoiTalkの通常利用を担う常用Team。", "enabled": True, "sort_order": 10, "activation": {"mode": "always", "contexts": []}, "subagent_ids": ["general_worker", "general_researcher", "docs_operator", "project_operator", "workspace_operator"]},
    "app_development": {"team_id": "app_development", "name": "App Development", "description": "アプリ開発の探索、設計、実装、レビュー。", "enabled": True, "sort_order": 20, "activation": {"mode": "contextual", "contexts": ["app_development"]}, "subagent_ids": ["code_explorer", "architecture_planner", "code_implementer", "code_reviewer"]},
    "story": {"team_id": "story", "name": "Story", "description": "Storyの執筆、取り込み、整合性・キャラクター口調レビュー。", "enabled": True, "sort_order": 30, "activation": {"mode": "contextual", "contexts": ["story"]}, "subagent_ids": ["story_writer", "story_consistency_reviewer", "character_voice_reviewer", "story_import", "general_worker"]},
}
# Internal migration only.  Not part of the persisted canonical graph.
AGENT_TEAM_DEFAULT_LLM_PROFILES: dict[str, dict[str, Any]] = {
    "heavy": {"profile_id": "heavy", "name": "高負荷", "target_type": "inherit", "provider": "", "model": "", "effort_policy": "same", "effort": "", "pool_id": "", "routing_profile_id": ""},
    "light": {"profile_id": "light", "name": "軽量", "target_type": "inherit", "provider": "", "model": "", "effort_policy": "lower", "effort": "", "pool_id": "", "routing_profile_id": ""},
    "coding": {"profile_id": "coding", "name": "コーディング", "target_type": "inherit", "provider": "", "model": "", "effort_policy": "same", "effort": "", "pool_id": "", "routing_profile_id": ""},
}
_CATALOG_LEGACY_LLM_PROFILE_IDS: dict[str, str] = {
    "general_worker": "heavy",
    "general_researcher": "light",
    "docs_operator": "light",
    "project_operator": "light",
    "workspace_operator": "coding",
    "code_explorer": "light",
    "architecture_planner": "heavy",
    "code_implementer": "coding",
    "code_reviewer": "light",
    "story_writer": "heavy",
    "story_consistency_reviewer": "light",
    "character_voice_reviewer": "light",
    "story_import": "light",
}
_SYSTEM_GLOBAL_EXECUTION_PROFILE_IDS = frozenset({"manual", "free-team"})
_DEFAULT_SEED_LLM_PROFILE_IDS = frozenset(AGENT_TEAM_DEFAULT_LLM_PROFILES)


def _id(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", text) else fallback


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    return getter(key, default) if callable(getter) else default


def _section(config: Any) -> dict[str, Any]:
    raw = _config_get(config, "agent_team", {}) if config is not None else {}
    return raw if isinstance(raw, dict) else {}


def _profile(profile_id: str, raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    target_type = str(raw.get("target_type") or "inherit").strip().lower()
    if target_type not in {"inherit", "static", "pool"}:
        target_type = "inherit"
    effort_policy = str(raw.get("effort_policy") or "same").strip().lower()
    if effort_policy not in {"same", "lower", "explicit", "default"}:
        effort_policy = "same"
    return {"profile_id": _id(profile_id), "name": str(raw.get("name") or profile_id).strip(), "target_type": target_type, "provider": str(raw.get("provider") or "").strip().lower(), "model": str(raw.get("model") or "").strip(), "effort_policy": effort_policy, "effort": str(raw.get("effort") or "").strip(), "pool_id": str(raw.get("pool_id") or "").strip(), "routing_profile_id": str(raw.get("routing_profile_id") or "").strip()}


def _subagent_normalized(subagent_id: str, raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    sid = _id(raw.get("subagent_id") or subagent_id)
    seed = AGENT_TEAM_SUBAGENT_CATALOG.get(sid, {})
    caps = raw.get("capability_ids") if isinstance(raw.get("capability_ids"), list) else seed.get("capability_ids", [])
    caps = list(dict.fromkeys(str(item).strip() for item in caps if str(item).strip() in AGENT_TEAM_CAPABILITY_CATALOG))
    access = str(raw.get("max_workspace_access") or seed.get("max_workspace_access") or "none").strip().lower()
    if access not in {"none", "read", "write"}:
        access = "none"
    scalable = bool(raw.get("scalable", seed.get("scalable", False)))
    try:
        max_instances = max(1, int(raw.get("max_instances", seed.get("max_instances", 1))))
    except (TypeError, ValueError):
        max_instances = 1
    if not scalable:
        max_instances = 1
    try:
        default_instances = int(raw.get("default_instances", seed.get("default_instances", 1)))
    except (TypeError, ValueError):
        default_instances = 1
    default_instances = max(0, min(default_instances, max_instances))
    instructions = str(raw.get("instructions") or seed.get("instructions") or "").strip()
    # Saved v3 configs retain user-editable instructions across upgrades.  The
    # hierarchy rule is a behavioral invariant rather than a preference, so
    # append it to older project_operator rows without replacing custom text.
    if sid == "project_operator" and "parent_task_id" not in instructions:
        instructions = f"{instructions} {PROJECT_OPERATOR_TASK_HIERARCHY_INSTRUCTIONS}".strip()
    if sid == "general_researcher" and (
        "docs_operator" not in instructions or "project_operator" not in instructions
    ):
        instructions = f"{instructions} {GENERAL_RESEARCHER_DELEGATION_INSTRUCTIONS}".strip()
    return {"subagent_id": sid, "name": str(raw.get("name") or raw.get("display_name") or seed.get("name") or sid).strip(), "description": str(raw.get("description") or seed.get("description") or "").strip(), "instructions": instructions, "enabled": bool(raw.get("enabled", seed.get("enabled", True))), "capability_ids": caps, "scalable": scalable, "default_instances": default_instances, "max_instances": max_instances, "max_workspace_access": access, "allow_cli_native_tools": bool(raw.get("allow_cli_native_tools", seed.get("allow_cli_native_tools", False)))}


def _activation(raw: Any) -> dict[str, Any]:
    """Normalize canonical Team activation metadata."""

    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "always").strip().lower()
    if mode not in {"always", "contextual", "manual"}:
        mode = "always"
    contexts = raw.get("contexts") if isinstance(raw.get("contexts"), list) else []
    return {
        "mode": mode,
        "contexts": list(
            dict.fromkeys(
                str(item).strip().lower()
                for item in contexts
                if str(item).strip()
            )
        ),
    }


def empty_execution_route() -> dict[str, Any]:
    return {
        "inherit_model": True,
        "provider": "",
        "model": "",
        "effort_policy": "same",
        "effort": "",
    }


def _execution_route(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    inherit = raw.get("inherit_model")
    if inherit is None:
        inherit_model = not (bool(provider) and bool(model))
    else:
        inherit_model = bool(inherit)
    if inherit_model:
        provider = ""
        model = ""
    elif not provider or not model:
        inherit_model = True
        provider = ""
        model = ""
    effort_policy = str(raw.get("effort_policy") or "same").strip().lower()
    effort = str(raw.get("effort") or "").strip()
    if inherit_model:
        if effort_policy not in {"same", "lower", "explicit", "default"}:
            effort_policy = "same"
        if effort_policy != "explicit":
            effort = ""
    else:
        # A provider/model route has two canonical effort states: an official
        # raw catalog value (``explicit``), or the target model's own default
        # (``default``).  Keep ``default`` intact so a route can deliberately
        # defer to the selected model instead of silently picking the first
        # catalog value.  Older ``same``/``lower`` records remain readable by
        # migrating them to the explicit shape when they carry an effort.
        if effort_policy == "default" or not effort:
            effort = ""
            effort_policy = "default"
        elif effort_policy != "explicit":
            effort_policy = "explicit"
    return {
        "inherit_model": inherit_model,
        "provider": provider,
        "model": model,
        "effort_policy": effort_policy,
        "effort": effort,
    }


def _team_execution_profile(
    profile_id: str,
    raw: Any,
    valid_subagents: set[str] | None = None,
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    pid = _id(raw.get("profile_id") or profile_id)
    default_raw = raw.get("default_route")
    if not default_raw:
        default_route = empty_execution_route()
    else:
        default_route = _execution_route(default_raw)
    overrides_raw = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
    overrides: dict[str, dict[str, Any]] = {}
    for key, item in overrides_raw.items():
        sid = _id(key)
        if not sid:
            continue
        if valid_subagents is not None and sid not in valid_subagents:
            continue
        overrides[sid] = _execution_route(item)
    return {
        "profile_id": pid,
        "name": str(raw.get("name") or raw.get("display_name") or pid).strip(),
        "enabled": bool(raw.get("enabled", True)),
        "default_route": default_route,
        "overrides": overrides,
    }


def _team_execution_profiles(
    raw: Any,
    valid_subagents: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        pid = _id(key)
        if not pid:
            continue
        result[pid] = _team_execution_profile(pid, item, valid_subagents)
    return result


def _legacy_llm_profile_id(subagent_id: str, raw: Any) -> str | None:
    raw = raw if isinstance(raw, dict) else {}
    if "llm_profile_id" in raw:
        value = raw.get("llm_profile_id")
        return _id(value) if value not in (None, "") else None
    catalog_id = _CATALOG_LEGACY_LLM_PROFILE_IDS.get(subagent_id)
    return catalog_id or None


def _is_default_seed_llm_profile(profile: dict[str, Any]) -> bool:
    profile_id = str(profile.get("profile_id") or "").strip()
    if profile_id not in _DEFAULT_SEED_LLM_PROFILE_IDS:
        return False
    seed = AGENT_TEAM_DEFAULT_LLM_PROFILES[profile_id]
    target = str(profile.get("target_type") or "inherit").strip().lower()
    if target != "inherit":
        return False
    if str(profile.get("provider") or "").strip() or str(profile.get("model") or "").strip():
        return False
    effort_policy = str(profile.get("effort_policy") or "same").strip().lower()
    return effort_policy == str(seed.get("effort_policy") or "same")


def _legacy_profile_to_execution_route(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile if isinstance(profile, dict) else {}
    target_type = str(profile.get("target_type") or "inherit").strip().lower()
    provider = str(profile.get("provider") or "").strip().lower()
    model = str(profile.get("model") or "").strip()
    inherit_model = True
    if target_type in {"static", "pool"} and provider and model:
        inherit_model = False
    return _execution_route(
        {
            "inherit_model": inherit_model,
            "provider": provider if not inherit_model else "",
            "model": model if not inherit_model else "",
            "effort_policy": profile.get("effort_policy") or "same",
            "effort": profile.get("effort") or "",
        }
    )


def _extract_user_global_execution_profiles(raw: Any) -> dict[str, dict[str, Any]]:
    raw = raw if isinstance(raw, dict) else {}
    if isinstance(raw.get("profiles"), dict):
        items = raw.get("profiles") or {}
    else:
        items = {
            key: value
            for key, value in raw.items()
            if key != "active_profile_id"
        }
    result: dict[str, dict[str, Any]] = {}
    for key, item in items.items():
        pid = _id(key)
        if not pid or pid in _SYSTEM_GLOBAL_EXECUTION_PROFILE_IDS:
            continue
        if not isinstance(item, dict):
            continue
        overrides = item.get("llm_profile_overrides")
        if not isinstance(overrides, dict) or not overrides:
            continue
        result[pid] = item
    return result


def _migrate_team_execution_profiles(
    *,
    team_subagent_ids: list[str],
    legacy_profile_by_subagent: dict[str, str | None],
    llm_profiles: dict[str, dict[str, Any]],
    global_execution_profiles: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    valid = set(team_subagent_ids)
    if global_execution_profiles:
        migrated: dict[str, dict[str, Any]] = {}
        for pid, item in global_execution_profiles.items():
            overrides_raw = item.get("llm_profile_overrides")
            overrides_raw = overrides_raw if isinstance(overrides_raw, dict) else {}
            overrides: dict[str, dict[str, Any]] = {}
            for key, override in overrides_raw.items():
                clean_key = _id(key)
                if not clean_key:
                    continue
                route = _legacy_profile_to_execution_route(
                    override if isinstance(override, dict) else {}
                )
                if clean_key in valid:
                    overrides[clean_key] = route
                    continue
                for sid in team_subagent_ids:
                    if legacy_profile_by_subagent.get(sid) == clean_key:
                        overrides[sid] = route
            migrated[pid] = _team_execution_profile(
                pid,
                {
                    "profile_id": pid,
                    "name": item.get("display_name") or item.get("name") or pid,
                    "enabled": item.get("enabled", True),
                    "default_route": empty_execution_route(),
                    "overrides": overrides,
                },
                valid,
            )
        return migrated

    custom_overrides: dict[str, dict[str, Any]] = {}
    for sid in team_subagent_ids:
        profile_id = legacy_profile_by_subagent.get(sid)
        if not profile_id:
            continue
        profile = llm_profiles.get(profile_id)
        if not profile or _is_default_seed_llm_profile(profile):
            continue
        custom_overrides[sid] = _legacy_profile_to_execution_route(profile)
    if not custom_overrides:
        return {}
    return {
        "migrated": _team_execution_profile(
            "migrated",
            {
                "profile_id": "migrated",
                "name": "migrated",
                "enabled": True,
                "default_route": empty_execution_route(),
                "overrides": custom_overrides,
            },
            valid,
        )
    }


def _team(team_id: str, raw: Any, valid_subagents: set[str] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    tid = _id(raw.get("team_id") or team_id)
    activation_value = raw.get("activation") if isinstance(raw.get("activation"), dict) else {}
    ids = raw.get("subagent_ids") if isinstance(raw.get("subagent_ids"), list) else []
    ids = list(dict.fromkeys(_id(item) for item in ids if _id(item)))
    if valid_subagents is not None:
        ids = [item for item in ids if item in valid_subagents]
    try:
        sort_order = int(raw.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    result = {
        "team_id": tid,
        "name": str(raw.get("name") or tid).strip(),
        "description": str(raw.get("description") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "sort_order": sort_order,
        "activation": _activation(activation_value),
        "subagent_ids": ids,
    }
    if "execution_profiles" in raw:
        # Override keys are limited to this Team's members, not every Subagent.
        result["execution_profiles"] = _team_execution_profiles(
            raw.get("execution_profiles"),
            set(ids),
        )
    return result


def normalize_agent_team_v3(
    value: Any,
    *,
    global_execution_profiles: Any = None,
) -> dict[str, Any]:
    """Normalize a canonical v3 graph without creating missing topology."""

    raw = value if isinstance(value, dict) else {}
    subagents_raw = raw.get("subagents") if isinstance(raw.get("subagents"), dict) else {}
    subagents = {
        sid: _subagent_normalized(sid, item)
        for sid, item in subagents_raw.items()
        if _id(sid)
    }
    legacy_profile_by_subagent = {
        sid: _legacy_llm_profile_id(sid, item if isinstance(item, dict) else {})
        for sid, item in subagents_raw.items()
        if _id(sid)
    }
    profiles_raw = raw.get("llm_profiles") if isinstance(raw.get("llm_profiles"), dict) else {}
    legacy_llm_profiles = {
        pid: _profile(pid, item)
        for pid, item in profiles_raw.items()
        if _id(pid)
    }
    user_global_eps = _extract_user_global_execution_profiles(global_execution_profiles)
    teams_raw = raw.get("teams") if isinstance(raw.get("teams"), dict) else {}
    teams: dict[str, dict[str, Any]] = {}
    for tid, item in teams_raw.items():
        if not _id(tid):
            continue
        raw_team = item if isinstance(item, dict) else {}
        team = _team(tid, raw_team, set(subagents))
        if "execution_profiles" not in raw_team:
            team["execution_profiles"] = _migrate_team_execution_profiles(
                team_subagent_ids=list(team.get("subagent_ids") or []),
                legacy_profile_by_subagent=legacy_profile_by_subagent,
                llm_profiles=legacy_llm_profiles,
                global_execution_profiles=user_global_eps,
            )
        teams[tid] = team
    return {
        "schema_version": AGENT_TEAM_SCHEMA_VERSION,
        "delegation_enabled": bool(raw.get("delegation_enabled", False)),
        "orchestration_mode": "director" if raw.get("orchestration_mode") == "director" else "standard",
        "teams": teams,
        "subagents": subagents,
    }


def agent_team_v3_enabled(config: Any) -> bool:
    """Return whether the loaded App Config uses canonical schema v3.

    This is a topology/version gate only; callers that need the user-facing
    delegation switch should use :func:`agent_team_v3_delegation_enabled`.
    """

    try:
        return int(_section(config).get("schema_version") or 0) >= AGENT_TEAM_SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def agent_team_v3_delegation_enabled(config: Any) -> bool:
    """Return the canonical v3 delegation switch without legacy fallbacks."""

    if not agent_team_v3_enabled(config):
        return False
    return bool(_section(config).get("delegation_enabled", False))


_EXTERNAL_APPROVAL_PROVIDERS = frozenset({
    "openai",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "gemini",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
})


def subagent_requires_external_approval(config: Any, subagent_id: str) -> bool:
    """Return whether a canonical Subagent route needs external approval."""

    clean = str(subagent_id or "").strip()
    route = resolve_agent_team_v3_route(config, clean) if clean else None
    provider = str((route or {}).get("provider") or "").strip().lower()
    return provider in _EXTERNAL_APPROVAL_PROVIDERS


def apply_subagent_mode(
    base_config: Any,
    subagent: dict[str, Any] | str | None,
    mode: str | None,
) -> dict[str, Any]:
    """Return an in-memory Subagent route with an explicit effort mode.

    Profiles remain the persisted source of provider/model/effort policy.  This
    helper only applies the caller's one-turn mode to a copied projection and
    never writes back to App Config.
    """

    if isinstance(subagent, str):
        clean = str(subagent).strip()
        value = next(
            (item for item in agent_team_v3_subagents(base_config, include_disabled=True)
             if str(item.get("subagent_id") or "") == clean),
            {},
        )
    else:
        value = subagent if isinstance(subagent, dict) else {}
    result = copy.deepcopy(value)
    sid = str(result.get("subagent_id") or "").strip()
    if sid and not any(key in result for key in ("provider", "model")):
        route = resolve_agent_team_v3_route(base_config, sid) or {}
        result.update(route)
    selected = str(mode or "").strip()
    if selected:
        result["effort"] = selected
        result["reasoning_effort"] = selected
    return result


def agent_team_capability_catalog() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(AGENT_TEAM_CAPABILITY_CATALOG)


def agent_team_v3_teams(config: Any) -> list[dict[str, Any]]:
    section = normalize_agent_team_v3(_section(config))
    return sorted((copy.deepcopy(item) for item in section["teams"].values()), key=lambda item: (int(item.get("sort_order") or 0), str(item.get("team_id"))))


def agent_team_v3_profiles(config: Any) -> list[dict[str, Any]]:
    """Compatibility alias. User-facing LLM Profiles are no longer canonical."""

    return []


def agent_team_v3_subagents(config: Any, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    section = normalize_agent_team_v3(_section(config))
    refs: dict[str, list[str]] = {sid: [] for sid in section["subagents"]}
    for tid, team in section["teams"].items():
        for sid in team.get("subagent_ids", []):
            if sid in refs:
                refs[sid].append(str(tid))
    result = []
    for sid, item in section["subagents"].items():
        if include_disabled or item.get("enabled"):
            projected = copy.deepcopy(item)
            projected["team_ids"] = sorted(refs.get(sid, []))
            result.append(projected)
    return sorted(result, key=lambda item: str(item.get("subagent_id")))


def _main_route(config: Any) -> dict[str, Any]:
    # Keep provider fallback policy in the service facade while avoiding a
    # module-import cycle; this function is called only after app bootstrap.
    from .agent_team_service import _main_route as resolve_main_route

    return resolve_main_route(config)


def _catalog_effort_options(provider: str, model: str) -> list[str]:
    from .llm_model_catalog import reasoning_effort_options_for_model

    return [
        str(item).strip()
        for item in reasoning_effort_options_for_model(provider, model)
        if str(item).strip()
    ]


def _apply_explicit_route_effort(result: dict[str, Any], requested: Any) -> dict[str, Any]:
    """Put a catalog-aligned explicit effort on the resolved route, or omit it.

    Clients must already see a value the resolved provider/model accepts.
    Unsupported names are dropped rather than remapped to a new default.
    The original name stays on ``requested_reasoning_effort`` for audit only.
    """

    requested_effort = str(requested or "").strip()
    provider_id = str(result.get("provider") or "").strip().lower()
    model_id = str(result.get("model") or "").strip()
    options = _catalog_effort_options(provider_id, model_id)
    result.pop("effort", None)
    result.pop("reasoning_effort", None)
    if requested_effort and requested_effort in options:
        result["effort"] = requested_effort
        result["reasoning_effort"] = requested_effort
        result.pop("requested_reasoning_effort", None)
        return result
    if requested_effort:
        result["requested_reasoning_effort"] = requested_effort
        logger.warning(
            "Dropping unsupported execution-route effort %s for %s/%s",
            requested_effort,
            provider_id or "(empty)",
            model_id or "(empty)",
        )
    else:
        result.pop("requested_reasoning_effort", None)
    return result


def _finalize_execution_route_effort(result: dict[str, Any]) -> dict[str, Any]:
    """Keep leftover Main ``mode`` from becoming an implicit request preset.

    Effective ``effort`` / ``reasoning_effort`` are the only values clients
    may send.  When those are empty, drop ``mode`` so a copied Main field
    cannot resurrect a default/dropped explicit choice.
    """

    effective = str(result.get("effort") or result.get("reasoning_effort") or "").strip()
    if not effective:
        result.pop("mode", None)
    return result


def _profile_route(profile: dict[str, Any], main: dict[str, Any]) -> dict[str, Any]:
    target_type = str(profile.get("target_type") or "inherit")
    # The root Main route is already resolved from ``llm_provider`` /
    # ``llm_model`` by ``agent_team_service._main_route``.  In particular, a
    # stale provider-specific key such as ``openai.model`` must not be read
    # again here and override an explicitly active Main model.  Static and
    # pool profiles are the only routes allowed to replace provider/model.
    result = dict(main)
    if target_type in {"static", "pool"}:
        if profile.get("provider"):
            result["provider"] = profile["provider"]
        if profile.get("model"):
            result["model"] = profile["model"]
    route_source = {
        "inherit": "main_inherit",
        "static": "static_profile",
        "pool": "pool_profile",
    }.get(target_type, "main_inherit")
    effort_policy = str(profile.get("effort_policy") or "same").strip().lower()
    result.update({
        "target_type": target_type,
        "profile_id": profile.get("profile_id"),
        "profile_name": profile.get("name"),
        "effort_policy": effort_policy,
        # Additive metadata consumed by AgentRun/TokenUsage-adjacent logs;
        # this does not introduce a DB column or alter the persisted schema.
        "route_source": route_source,
    })
    if target_type == "pool":
        result["pool_id"] = profile.get("pool_id") or None
        result["routing_profile_id"] = profile.get("routing_profile_id") or None
    # Execution Route owns effort against the resolved provider/model.
    # inherit+same/lower stay relative to Chat Main.  explicit keeps the
    # saved official name only when that catalog accepts it; otherwise the
    # resolver drops it here so CLI/API clients never see an incompatible
    # value.  Legacy static/pool same/lower must not copy Main's effort
    # onto a different model.
    if effort_policy == "explicit":
        _apply_explicit_route_effort(result, profile.get("effort"))
    elif effort_policy == "default" or (
        target_type in {"static", "pool"} and effort_policy in {"same", "lower"}
    ):
        result.pop("effort", None)
        result.pop("reasoning_effort", None)
        result.pop("requested_reasoning_effort", None)
    elif effort_policy == "lower":
        current = str(result.get("effort") or result.get("reasoning_effort") or "").strip().lower()
        provider_id = str(result.get("provider") or "").strip().lower()
        model_id = str(result.get("model") or "").strip()
        options = [
            str(item).strip().lower()
            for item in _catalog_effort_options(provider_id, model_id)
        ]
        if current and options and current in options:
            index = options.index(current)
            if index > 0:
                lower = options[index - 1]
                result["effort"] = lower
                result["reasoning_effort"] = lower
        result.pop("requested_reasoning_effort", None)
    else:
        result.pop("requested_reasoning_effort", None)
    _finalize_execution_route_effort(result)
    provider = str(result.get("provider") or "").lower()
    result["backend"] = "cli" if provider.endswith("-cli") else "api"
    return result


def _apply_execution_route(route: dict[str, Any] | None, main: dict[str, Any]) -> dict[str, Any]:
    route = _execution_route(route) if route else empty_execution_route()
    profile = {
        "profile_id": "",
        "name": "",
        "target_type": "inherit" if route.get("inherit_model", True) else "static",
        "provider": route.get("provider") or "",
        "model": route.get("model") or "",
        "effort_policy": route.get("effort_policy") or "same",
        "effort": route.get("effort") or "",
    }
    applied = _profile_route(profile, main)
    applied["inherit_model"] = bool(route.get("inherit_model", True))
    applied.pop("profile_id", None)
    applied.pop("profile_name", None)
    return applied


def _session_uses_team_execution_profile() -> tuple[str, str]:
    from .session_llm_runtime_context import (
        session_agent_team_selection,
        session_execution_profile_id,
        session_main_route_override,
    )

    profile_id = str(session_execution_profile_id() or "").strip()
    if not profile_id:
        return "", ""
    selection = session_agent_team_selection() or {}
    if str(selection.get("mode") or "auto").strip().lower() != "fixed":
        return "", ""
    team_id = str(selection.get("team_id") or "").strip()
    if not team_id:
        return "", ""
    override = session_main_route_override() or {}
    if (
        str(override.get("provider") or "").strip().lower() == "routing-profile"
        and str(override.get("model") or "").strip() == "free-team"
    ):
        return "", ""
    return team_id, profile_id


def resolve_team_execution_route(
    config: Any,
    subagent_id: str,
    *,
    team_id: str | None = None,
    execution_profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the Team ExecutionRoute for a member, or None to use Main as-is."""

    clean_subagent = str(subagent_id or "").strip()
    if not clean_subagent:
        return None
    selected_team_id = str(team_id or "").strip()
    selected_profile_id = str(execution_profile_id or "").strip()
    if not selected_team_id or not selected_profile_id:
        selected_team_id, selected_profile_id = _session_uses_team_execution_profile()
    if not selected_team_id or not selected_profile_id:
        return None
    team = next(
        (
            item
            for item in agent_team_v3_teams(config)
            if str(item.get("team_id") or "") == selected_team_id
        ),
        None,
    )
    if not team or not team.get("enabled", True):
        return None
    members = {
        str(item).strip()
        for item in (team.get("subagent_ids") or [])
        if str(item).strip()
    }
    if clean_subagent not in members:
        return None
    profiles = team.get("execution_profiles") if isinstance(team.get("execution_profiles"), dict) else {}
    profile = profiles.get(selected_profile_id)
    if not isinstance(profile, dict) or not profile.get("enabled", True):
        return None
    overrides = profile.get("overrides") if isinstance(profile.get("overrides"), dict) else {}
    if clean_subagent in overrides:
        return _execution_route(overrides.get(clean_subagent))
    default_route = profile.get("default_route")
    if not default_route:
        return None
    return _execution_route(default_route)


def resolve_agent_team_v3_route(config: Any, subagent_id: str, *, main_route: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from .execution_profile_service import resolve_execution_main_route

    subagent = next((item for item in agent_team_v3_subagents(config) if item.get("subagent_id") == str(subagent_id)), None)
    if not subagent:
        return None
    route_main = dict(main_route or resolve_execution_main_route(config))
    exec_route = resolve_team_execution_route(config, str(subagent_id))
    if exec_route is None:
        route = dict(route_main)
        provider = str(route.get("provider") or "").strip().lower()
        model = str(route.get("model") or "").strip()
        route.update({
            "target_type": "inherit",
            "inherit_model": True,
            "effort_policy": "same",
            "route_source": "main_inherit",
            "backend": "cli" if provider.endswith("-cli") else "api",
        })
        team_id, profile_id = _session_uses_team_execution_profile()
        route["execution_profile_id"] = profile_id or None
        route.pop("llm_profile_id", None)
        route.pop("profile_id", None)
        route.pop("profile_name", None)
    else:
        route = _apply_execution_route(exec_route, route_main)
        _team_id, profile_id = _session_uses_team_execution_profile()
        route["execution_profile_id"] = profile_id or None
        route.pop("llm_profile_id", None)
        provider = str(route.get("provider") or "").strip().lower()
        model = str(route.get("model") or "").strip()
    if not provider or not model:
        return None
    if provider == "routing-profile" and model == "free-team":
        route["pool_id"] = route.get("pool_id") or str(_config_get(config, "routing_profiles.free-team.main_pool_id", "coordinator") or "coordinator")
        route["routing_profile_id"] = route.get("routing_profile_id") or "free-team"
    route.update({
        "provider": provider,
        "model": model,
        "subagent_id": subagent["subagent_id"],
    })
    return route


def resolve_agent_execution_backend(
    config: Any,
    subagent_id: str,
    *,
    requested_work_mode: str = "read",
) -> dict[str, Any] | None:
    clean_id = str(subagent_id or "").strip()
    subagent = next(
        (item for item in agent_team_v3_subagents(config)
         if str(item.get("subagent_id") or "") == clean_id),
        None,
    )
    if not subagent or not subagent.get("enabled"):
        return None
    route = resolve_agent_team_v3_route(config, clean_id)
    if not route:
        return None
    mode = str(requested_work_mode or "read").strip().lower()
    if mode not in {"read", "write"}:
        mode = "read"
    access = agent_team_workspace_access(subagent)
    if mode == "write" and not agent_team_subagent_allows_write(subagent):
        return None
    backend = str(route.get("backend") or "api")
    if backend == "cli" and not bool(subagent.get("allow_cli_native_tools")):
        return None
    capabilities = filter_agent_team_capabilities(
        subagent, work_mode=mode, backend=backend
    )
    return {
        **route,
        "backend": backend,
        "work_mode": mode,
        "workspace_access": access,
        "capabilities": capabilities,
        "native_tools": backend == "cli" and bool(subagent.get("allow_cli_native_tools")),
        "managed_tool_bridge": True,
    }


def agent_team_workspace_access(subagent: dict[str, Any] | None) -> str:
    """Return the native/filesystem ceiling, with a legacy declaration fallback."""

    subagent = subagent if isinstance(subagent, dict) else {}
    configured = str(subagent.get("max_workspace_access") or "").strip().lower()
    if configured in {"none", "read", "write"}:
        return configured
    declared = {
        str(item).strip()
        for item in (subagent.get("capability_ids") or [])
        if str(item).strip()
    }
    if declared & {"workspace_write", "command_execute"}:
        return "write"
    if declared & {"workspace_read", "repo_map"}:
        return "read"
    return "none"


def agent_team_subagent_allows_write(subagent: dict[str, Any] | None) -> bool:
    """Whether write work is possible through managed or workspace capabilities.

    ``max_workspace_access`` limits filesystem/provider-native access only.  A
    Project or Docs operator can therefore mutate AoiTalk data with managed
    high-level tools while retaining a read/none workspace ceiling.
    """

    subagent = subagent if isinstance(subagent, dict) else {}
    workspace_access = agent_team_workspace_access(subagent)
    for capability_id in subagent.get("capability_ids") or []:
        capability = AGENT_TEAM_CAPABILITY_CATALOG.get(str(capability_id).strip())
        if not capability or capability.get("access") != "write":
            continue
        if capability.get("family") != "workspace" or workspace_access == "write":
            return True
    return False


def filter_agent_team_capabilities(subagent: dict[str, Any] | None, requested: Any = None, *, work_mode: str = "read", backend: str = "api") -> list[str]:
    subagent = subagent if isinstance(subagent, dict) else {}
    declared = subagent.get("capability_ids") if isinstance(subagent.get("capability_ids"), list) else []
    allowed = {str(item).strip() for item in declared if str(item).strip() in AGENT_TEAM_CAPABILITY_CATALOG}
    # Shared read-only tools (currently Web Search) are available to every
    # Subagent without duplicating the same capability ID in every definition.
    allowed.update(AGENT_TEAM_SHARED_READ_CAPABILITIES)
    if requested is not None:
        allowed &= {str(item).strip() for item in requested if str(item).strip()}
    if str(work_mode or "read").lower() != "write":
        allowed = {item for item in allowed if AGENT_TEAM_CAPABILITY_CATALOG[item].get("access") != "write"}
    workspace_access = agent_team_workspace_access(subagent)
    if workspace_access == "none":
        allowed = {
            item
            for item in allowed
            if AGENT_TEAM_CAPABILITY_CATALOG[item].get("family") != "workspace"
        }
    elif workspace_access == "read":
        allowed = {
            item
            for item in allowed
            if not (
                AGENT_TEAM_CAPABILITY_CATALOG[item].get("family") == "workspace"
                and AGENT_TEAM_CAPABILITY_CATALOG[item].get("access") == "write"
            )
        }
    if str(backend or "api").lower() != "cli":
        allowed = {item for item in allowed if not AGENT_TEAM_CAPABILITY_CATALOG[item].get("native") or item == "repo_map"}
    return sorted(allowed)


def agent_team_v3_context_tags(*, user: Any = None, project: Any = None, session: Any = None, generation_profile: str | None = None, app_target_id: str | None = None, development_status: str | None = None, story_mode: str | None = None, context_tags: Any = None, trpg_context: bool | None = None) -> set[str]:
    tags = {str(item).strip().lower() for item in (context_tags or []) if str(item).strip()} if isinstance(context_tags, (list, tuple, set, frozenset)) else set()
    def _read(obj: Any, *names: str) -> Any:
        if isinstance(obj, dict):
            for name in names:
                if obj.get(name) is not None:
                    return obj.get(name)
            return None
        for name in names:
            value = getattr(obj, name, None)
            if value is not None:
                return value
        return None

    session_map = session if isinstance(session, dict) else {}
    app_context_raw = _read(project, "app_context")
    app_context = app_context_raw if isinstance(app_context_raw, dict) else {}
    project_metadata = _read(project, "metadata", "project_metadata")
    project_metadata = project_metadata if isinstance(project_metadata, dict) else {}
    metadata_app_context = project_metadata.get("app_context")
    metadata_app_context = metadata_app_context if isinstance(metadata_app_context, dict) else {}
    session_app_context_raw = _read(session, "app_context")
    session_app_context = session_app_context_raw if isinstance(session_app_context_raw, dict) else {}
    target = (
        app_target_id
        or _read(project, "app_target_id", "app_id", "target_id")
        or app_context.get("target_key")
        or project_metadata.get("app_target_id")
        or project_metadata.get("app_id")
        or metadata_app_context.get("target_key")
        or _read(session, "app_target_id", "app_id")
        or session_app_context.get("target_key")
        or _read(user, "app_target_id")
    )
    status = str(
        development_status
        or _read(project, "development_status", "status")
        or project_metadata.get("development_status")
        or project_metadata.get("status")
        or _read(session, "development_status", "status")
        or ""
    ).strip().lower()
    if target and (not status or status in {"planning", "active", "in_progress", "review", "development", "working"}):
        tags.add("app_development")
    story_value = story_mode
    if story_value is None:
        story_value = _read(session, "story_mode", "story_context") or _read(project, "story_mode")
    session_mode = _read(session, "mode")
    session_context_type = _read(session, "context_type")
    session_agent_name = _read(session, "agent_name")
    session_allowed_tools = _read(session, "allowed_tools")
    session_type_name = type(session).__name__.lower() if session is not None else ""
    story_session_shape = (
        "storywriting" in session_type_name
        or "storychat" in session_type_name
        or (session_agent_name and "story" in str(session_agent_name).lower())
        or (
            isinstance(session_allowed_tools, (list, tuple, set, frozenset))
            and any("writing" in str(item).lower() for item in session_allowed_tools)
        )
    )
    if story_value or story_session_shape or any("story" in str(value).lower() for value in (generation_profile, session_mode, session_context_type) if value):
        tags.add("story")
    # Character/TRPG runtime is deliberately not inferred from ordinary
    # conversation fields.  Integrations with a trustworthy TRPG session
    # source can opt in through this narrow extension hook.
    if trpg_context is True:
        tags.add("trpg")
    return tags


def agent_team_scope_active(config: Any, *, user: Any = None, project: Any = None, session: Any = None, generation_profile: str | None = None, app_target_id: str | None = None, development_status: str | None = None, story_mode: str | None = None, context_tags: Any = None, loaded_team_ids: Any = None, trpg_context: bool | None = None) -> dict[str, Any]:
    from .session_llm_runtime_context import session_agent_team_selection

    selection = session_agent_team_selection()
    if isinstance(selection, dict) and str(selection.get("mode") or "auto").strip().lower() == "fixed":
        fixed_team_id = str(selection.get("team_id") or "").strip()
        teams = {
            str(team.get("team_id") or ""): team
            for team in agent_team_v3_teams(config)
            if str(team.get("team_id") or "").strip()
        }
        team = teams.get(fixed_team_id)
        if team and team.get("enabled", True):
            loaded_source = loaded_team_ids
            if isinstance(selection, dict):
                selection_loaded = selection.get("loaded_team_ids") or []
                if selection_loaded:
                    loaded_source = selection_loaded
            loaded = {
                str(item).strip()
                for item in (loaded_source or [])
                if str(item).strip()
            }
            active_team_ids = [fixed_team_id]
            active_subagent_ids = {
                str(item).strip()
                for item in (team.get("subagent_ids") or [])
                if str(item).strip()
            }
            requires_load: list[str] = []
            for candidate in agent_team_v3_teams(config):
                if not candidate.get("enabled", True):
                    continue
                candidate_id = str(candidate.get("team_id") or "").strip()
                if not candidate_id or candidate_id == fixed_team_id:
                    continue
                activation = candidate.get("activation") or {}
                mode = str(activation.get("mode") or "always").lower()
                if mode == "manual" and candidate_id in loaded:
                    active_team_ids.append(candidate_id)
                    active_subagent_ids.update(candidate.get("subagent_ids") or [])
                elif mode == "manual":
                    requires_load.append(candidate_id)
            return {
                "context_tags": sorted(
                    agent_team_v3_context_tags(
                        user=user,
                        project=project,
                        session=session,
                        generation_profile=generation_profile,
                        app_target_id=app_target_id,
                        development_status=development_status,
                        story_mode=story_mode,
                        context_tags=context_tags,
                        trpg_context=trpg_context,
                    )
                ),
                "active_team_ids": active_team_ids,
                "requires_load": requires_load,
                "active_subagent_ids": sorted(active_subagent_ids),
                "reason": "fixed",
            }
        return {
            "context_tags": [],
            "active_team_ids": [],
            "requires_load": [],
            "active_subagent_ids": [],
            "reason": "fixed_unavailable",
        }

    tags = agent_team_v3_context_tags(user=user, project=project, session=session, generation_profile=generation_profile, app_target_id=app_target_id, development_status=development_status, story_mode=story_mode, context_tags=context_tags, trpg_context=trpg_context)
    loaded_source = loaded_team_ids
    if isinstance(selection, dict) and str(selection.get("mode") or "auto").strip().lower() == "auto":
        selection_loaded = selection.get("loaded_team_ids") or []
        if selection_loaded:
            loaded_source = selection_loaded
    loaded = {str(item).strip() for item in (loaded_source or []) if str(item).strip()}
    active_team_ids: list[str] = []
    requires_load: list[str] = []
    active_subagent_ids: set[str] = set()
    for team in agent_team_v3_teams(config):
        if not team.get("enabled", True):
            continue
        activation = team.get("activation") or {}
        mode = str(activation.get("mode") or "always").lower()
        contexts = {str(item).lower() for item in activation.get("contexts", []) if str(item).strip()}
        active = mode == "always" or (mode == "contextual" and bool(contexts & tags)) or (mode == "manual" and str(team.get("team_id")) in loaded)
        if active:
            active_team_ids.append(str(team.get("team_id")))
            active_subagent_ids.update(team.get("subagent_ids") or [])
        elif mode == "manual":
            requires_load.append(str(team.get("team_id")))
    return {"context_tags": sorted(tags), "active_team_ids": active_team_ids, "requires_load": requires_load, "active_subagent_ids": sorted(active_subagent_ids), "reason": "active" if active_team_ids else "none"}


# Canonical short names used by runtime/UI integrations.  The explicit v3
# names above remain available for migration-aware callers.
def agent_team_teams(config: Any) -> list[dict[str, Any]]:
    return agent_team_v3_teams(config)


def agent_team_subagents(config: Any, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    return agent_team_v3_subagents(config, include_disabled=include_disabled)


def agent_team_subagent(config: Any, subagent_id: str) -> dict[str, Any] | None:
    clean = str(subagent_id or "").strip()
    return next((item for item in agent_team_v3_subagents(config, include_disabled=True) if str(item.get("subagent_id") or "") == clean), None)


def agent_team_llm_profiles(config: Any) -> list[dict[str, Any]]:
    return agent_team_v3_profiles(config)


def resolve_subagent_route(config: Any, subagent_id: str, *, main_route: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return resolve_agent_team_v3_route(config, subagent_id, main_route=main_route)


def resolve_agent_team_scope(config: Any, **kwargs: Any) -> dict[str, Any]:
    return agent_team_scope_active(config, **kwargs)


def filter_subagent_capabilities(subagent: dict[str, Any] | None, requested: Any = None, *, work_mode: str = "read", backend: str = "api") -> list[str]:
    return filter_agent_team_capabilities(subagent, requested=requested, work_mode=work_mode, backend=backend)


def agent_team_v3_visible_subagents(config: Any, *, context_tags: Any = None, loaded_team_ids: Any = None, include_disabled: bool = False, user: Any = None, project: Any = None, session: Any = None, trpg_context: bool | None = None) -> list[dict[str, Any]]:
    scope = agent_team_scope_active(
        config,
        context_tags=context_tags,
        loaded_team_ids=loaded_team_ids,
        user=user,
        project=project,
        session=session,
        trpg_context=trpg_context,
    )
    visible = set(scope.get("active_subagent_ids") or [])
    return [item for item in agent_team_v3_subagents(config, include_disabled=include_disabled) if item.get("subagent_id") in visible]
