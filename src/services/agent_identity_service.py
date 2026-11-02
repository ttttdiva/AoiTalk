"""Application services for the WS01 generic Agent identity foundation.

The service owns normalization, idempotency, immutable revision creation, and
the explicit assignment relations.  API callers should use this layer rather
than mutating the ORM rows directly so a client payload can never become an
authority grant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from ..features import Features
from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentOrganizationProfile,
    AgentProjectGrant,
    AgentRevision,
    AgentSpaceAssignment,
    AgentTaskAssignment,
    Organization,
    Persona,
    PersonaOperatorAssignment,
    Project,
    Space,
    Task,
    User,
)
from .agent_capabilities import AGENT_CAPABILITY_CATALOG
from .project_permissions import (
    PROJECT_PERMISSION_KEYS,
    get_default_project_permissions,
    normalize_project_member_permissions,
    normalize_project_member_role,
)

logger = logging.getLogger(__name__)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "cookie",
)
_MAX_POLICY_KEYS = 24
_MAX_POLICY_VALUE_LENGTH = 512
_MAX_LIST_ITEMS = 64
_OMITTED = object()

# P2 keeps the role vocabulary in the identity projection.  The resolver that
# understands the application-wide model_roles table lives in the follow-up
# agent_revision_v2 module; this list lets the API expose a stable shape even
# while that resolver is not installed yet.
_MODEL_ROLES = (
    "chat",
    "summarize",
    "code",
    "review",
    "research",
)
_DELEGATE_WORKER_KINDS = frozenset({"codex", "antigravity", "claude", "grok", "muse"})
_MODEL_OVERRIDE_ROLES = frozenset(_MODEL_ROLES)

_AGENT_TOOL_LABELS = {
    "workspace_read": "Workspace 読み取り",
    "workspace_write": "Workspace 書き込み",
    "repo_map": "リポジトリ構造",
    "command_execute": "コマンド実行",
    "aoi_tools": "AoiTalk 基本ツール",
    "docs_read": "Docs 読み取り",
    "docs_write": "Docs 書き込み",
    "project_read": "Project 読み取り",
    "project_write": "Project 書き込み",
    "web_read": "Web 調査",
    "browser_qa": "ブラウザ QA",
    "media": "メディア操作",
    "spotify": "Spotify 操作",
    "external_action_propose": "外部操作の提案",
    "remote_agent_delegate": "リモート Agent 委任",
    "personal_deadline_read": "個人期限の読み取り",
    "personal_deadline_draft": "個人期限の下書き",
    "personal_inventory_read": "個人在庫の読み取り",
    "personal_inventory_draft": "個人在庫の下書き",
    "personal_finance_read": "個人家計の読み取り",
    "personal_career_context": "個人キャリア文脈",
}

_AGENT_TOOL_DESCRIPTIONS = {
    "workspace_read": "許可された Workspace のファイルや検索結果を読み取ります。",
    "workspace_write": "許可された Workspace のファイルを変更します。",
    "repo_map": "許可されたリポジトリの構造を調査します。",
    "command_execute": "許可された範囲でコマンドを実行します。",
    "aoi_tools": "AoiTalk の Agent 状態確認、委任、質問などを使います。",
    "docs_read": "AoiTalk Docs を検索・読み取ります。",
    "docs_write": "AoiTalk Docs を作成・更新・整理します。",
    "project_read": "Project、Task、Calendar などを読み取ります。",
    "project_write": "Project、Task、Calendar などを更新します。",
    "web_read": "Web 検索や外部ページの読み取りを行います。",
    "browser_qa": "発行された QA 範囲内でブラウザを操作・確認します。",
    "media": "許可されたメディア情報を読み取り、操作します。",
    "spotify": "Spotify の検索や再生操作を行います。",
    "external_action_propose": "登録済み ActionPolicy に従う外部操作を提案します。",
    "remote_agent_delegate": "許可されたリモート Agent へ作業を委任します。",
    "personal_deadline_read": "本人の期限情報を読み取ります。",
    "personal_deadline_draft": "本人の期限候補を未受理の下書きにします。",
    "personal_inventory_read": "本人の在庫情報を読み取ります。",
    "personal_inventory_draft": "本人の在庫候補を未受理の下書きにします。",
    "personal_finance_read": "本人の家計情報を読み取ります。",
    "personal_career_context": "本人が選択したキャリア文脈を読み取ります。",
}

_AGENT_TOOL_GROUPS = (
    {
        "id": "chat_default",
        "label": "普通の Chat の tool すべて",
        "description": "普通の Chat で使える tool をそのまま公開します（新しく増えた tool も含みます）。実行できる範囲は権限上限で決まります。",
        "capability_ids": [],
    },
    {
        "id": "browser",
        "label": "ブラウザ操作",
        "description": "発行されたブラウザ QA 範囲で確認操作を行います。",
        "capability_ids": ["browser_qa"],
    },
    {
        "id": "delegate",
        "label": "Agent 委任",
        "description": "許可された Agent に作業を委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_media",
        "label": "メディア委任",
        "description": "保存済みチームグラフへメディア制作を委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_plan",
        "label": "委任計画",
        "description": "許可された範囲で正式な委任計画を作成します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_worker:codex",
        "label": "Codex 委任",
        "description": "許可された範囲の作業を Codex に委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_worker:antigravity",
        "label": "Antigravity 委任",
        "description": "許可された範囲の作業を Antigravity に委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_worker:claude",
        "label": "Claude 委任",
        "description": "許可された範囲の作業を Claude に委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_worker:grok",
        "label": "Grok 委任",
        "description": "許可された範囲の作業を Grok に委任します。",
        "capability_ids": ["aoi_tools"],
    },
    {
        "id": "delegate_worker:muse",
        "label": "Muse 委任",
        "description": "許可された条件で Muse への委任候補を公開します。",
        "capability_ids": ["remote_agent_delegate"],
    },
)

# ``AgentPrivateRoot.purpose`` is intentionally reserved for the existing
# AoiDots dialogue root.  The ordinary Chat default Agent therefore uses the
# same owner-scoped private identity convention as the AoiDots worker, with a
# stable kind in its idempotency key.  The key is the durable identity marker;
# display names and slugs remain presentation data.
AOI_DEFAULT_AGENT_KIND = "chat_default"
AOI_DEFAULT_AGENT_KEY_VERSION = "v1"
_P2_UNSET = object()


def aoi_default_agent_idempotency_key(owner_user_id: Any) -> str:
    """Return the stable owner key for the default Chat Agent."""

    return f"private-agent:{AOI_DEFAULT_AGENT_KIND}:{owner_user_id}:{AOI_DEFAULT_AGENT_KEY_VERSION}"


def _set_optional_revision_value(row: Any, name: str, value: Any) -> bool:
    """Set a P2 revision field when the expand-only column is available."""

    for candidate in (name, f"{name}_json"):
        if hasattr(row, candidate):
            setattr(row, candidate, value)
            return True
    return False


def _optional_revision_value(row: Any, name: str, default: Any = None) -> Any:
    """Read an expand-only P2 field, including legacy JSON aliases."""

    for candidate in (name, f"{name}_json"):
        if isinstance(row, Mapping):
            if candidate in row:
                return row.get(candidate)
        elif hasattr(row, candidate):
            return getattr(row, candidate, default)
    return default


def _normalize_optional_revision_value(name: str, value: Any) -> Any:
    """Validate the small P2 shapes accepted by internal revision writers."""

    if value is None:
        return None
    if name == "tool_ids":
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise AgentIdentityValidationError("tool_ids must be a list")
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if not isinstance(value, Mapping):
        raise AgentIdentityValidationError(f"{name} must be an object")
    return dict(value)


def _revision_p2_column_available(name: str) -> bool:
    """Return whether this process has the corresponding P2 ORM column."""

    return any(hasattr(AgentRevision, candidate) for candidate in (name, f"{name}_json"))


def _effective_revision_p2_value(
    *,
    is_default_chat_agent: bool,
    existing: Any,
    latest_existing: Any,
    name: str,
    requested: Any,
) -> Any:
    """Resolve one P2 value, preferring an idempotency winner over latest."""

    if requested is not _P2_UNSET:
        return _normalize_optional_revision_value(name, requested)
    source = existing if existing is not None else latest_existing
    if is_default_chat_agent and source is not None:
        return _optional_revision_value(source, name)
    return None

# Ceiling is derived only from the four existing personal adapter profiles.
# The owner route never accepts arbitrary subagents, tools, wake policies, or
# provider/model configuration from a request.
_PERSONAL_ADAPTER_PROFILE_CAPABILITIES = {
    "personal-firearms-admin": ("personal_deadline_read", "personal_deadline_draft"),
    "personal-ammunition-bookkeeper": ("personal_inventory_read", "personal_inventory_draft"),
    "personal-finance-reviewer": ("personal_finance_read",),
    "personal-career-planner": ("personal_career_context",),
}


class AgentIdentityError(Exception):
    """Base service error with a stable HTTP-facing status code."""

    status_code = 400

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class AgentIdentityValidationError(AgentIdentityError):
    status_code = 422


class AgentIdentityNotFoundError(AgentIdentityError):
    status_code = 404


class AgentIdentityConflictError(AgentIdentityError):
    status_code = 409


class AgentIdentityAuthorizationError(AgentIdentityError):
    status_code = 403


class AgentIdentityFeatureDisabledError(AgentIdentityError):
    status_code = 404


def canonical_json(value: Any) -> str:
    """Encode a bounded JSON value deterministically for content hashes."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AgentIdentityValidationError("value must be JSON serializable") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _uuid(value: Any, label: str, *, required: bool = True) -> UUID | None:
    if value in (None, "") and not required:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentIdentityValidationError(f"invalid {label}") from exc


def _text(value: Any, label: str, *, max_length: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text and required:
        raise AgentIdentityValidationError(f"{label} is required")
    if len(text) > max_length:
        raise AgentIdentityValidationError(f"{label} is too long")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise AgentIdentityValidationError(f"{label} contains control characters")
    return text


def _idempotency(value: Any, label: str = "idempotency_key") -> str:
    text = _text(value, label, max_length=255)
    if not _ID_RE.fullmatch(text):
        raise AgentIdentityValidationError(f"invalid {label}")
    return text


def _safe_policy(value: Any, *, label: str, allowed_keys: Iterable[str]) -> dict[str, Any]:
    """Validate a closed, bounded policy object.

    Unknown/secret-shaped keys are rejected rather than silently ignored.  A
    policy is configuration evidence; the authority resolver still applies
    every independent boundary as an intersection.
    """

    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise AgentIdentityValidationError(f"{label} must be an object")
    allowed = set(allowed_keys)
    if len(value) > _MAX_POLICY_KEYS:
        raise AgentIdentityValidationError(f"{label} has too many fields")
    result: dict[str, Any] = {}
    for raw_key, raw_item in value.items():
        key = str(raw_key).strip()
        if key not in allowed:
            raise AgentIdentityValidationError(f"unsupported {label} field: {key or '(empty)'}")
        if any(marker in key.casefold() for marker in _SECRET_KEY_MARKERS):
            raise AgentIdentityValidationError(f"secret-bearing {label} field is not allowed")
        if isinstance(raw_item, str):
            if len(raw_item) > _MAX_POLICY_VALUE_LENGTH:
                raise AgentIdentityValidationError(f"{label}.{key} is too long")
            result[key] = raw_item
        elif isinstance(raw_item, bool):
            result[key] = raw_item
        elif isinstance(raw_item, int) and not isinstance(raw_item, bool):
            if raw_item < 0 or raw_item > 1_000_000_000:
                raise AgentIdentityValidationError(f"{label}.{key} is out of range")
            result[key] = raw_item
        elif isinstance(raw_item, (list, tuple)):
            if len(raw_item) > _MAX_LIST_ITEMS:
                raise AgentIdentityValidationError(f"{label}.{key} has too many items")
            clean: list[Any] = []
            for item in raw_item:
                if not isinstance(item, (str, int, bool)):
                    raise AgentIdentityValidationError(f"{label}.{key} has an invalid item")
                if isinstance(item, str) and len(item) > _MAX_POLICY_VALUE_LENGTH:
                    raise AgentIdentityValidationError(f"{label}.{key} has an item that is too long")
                clean.append(item)
            result[key] = list(dict.fromkeys(clean))
        elif isinstance(raw_item, Mapping):
            # Only the explicitly supported permissions map may be nested.
            # Arbitrary recursive maps would let secret-bearing values hide
            # behind innocuous keys and would later be echoed by safe DTOs.
            if key != "permissions":
                raise AgentIdentityValidationError(
                    f"{label}.{key} must be a flat list/scalar"
                )
            if len(raw_item) > len(PROJECT_PERMISSION_KEYS):
                raise AgentIdentityValidationError(
                    f"{label}.{key} has too many fields"
                )
            nested: dict[str, bool] = {}
            for nested_key, nested_value in raw_item.items():
                normalized_key = str(nested_key).strip()
                if (
                    normalized_key not in PROJECT_PERMISSION_KEYS
                    or not isinstance(nested_value, bool)
                ):
                    raise AgentIdentityValidationError(
                        f"invalid {label}.{key} entry"
                    )
                nested[normalized_key] = nested_value
            result[key] = nested
        else:
            raise AgentIdentityValidationError(f"{label}.{key} has an unsupported value")
    return result


def _bounded_json_value(value: Any, *, label: str, depth: int = 0) -> Any:
    """Validate bounded P2 JSON without accepting credential-shaped keys."""

    if depth > 6:
        raise AgentIdentityValidationError(f"{label} is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_POLICY_VALUE_LENGTH:
            raise AgentIdentityValidationError(f"{label} is too long")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_POLICY_KEYS:
            raise AgentIdentityValidationError(f"{label} has too many fields")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, f"{label} key", max_length=100)
            if any(marker in key.casefold() for marker in _SECRET_KEY_MARKERS):
                raise AgentIdentityValidationError(f"secret-bearing {label} field is not allowed")
            result[key] = _bounded_json_value(raw_value, label=f"{label}.{key}", depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > _MAX_LIST_ITEMS:
            raise AgentIdentityValidationError(f"{label} has too many items")
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [
            _bounded_json_value(item, label=f"{label} item", depth=depth + 1)
            for item in items
        ]
    raise AgentIdentityValidationError(f"{label} has an unsupported value")


def _strict_route(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentIdentityValidationError(f"{label} must be an object")
    kind = str(value.get("kind") or "").strip().casefold()
    if kind == "pool":
        if set(value) != {"kind", "pool_id"}:
            raise AgentIdentityValidationError(f"{label} has unsupported fields")
        pool_id = _text(value.get("pool_id"), f"{label}.pool_id", max_length=160)
        if not pool_id:
            raise AgentIdentityValidationError(f"{label}.pool_id is required")
        return {"kind": "pool", "pool_id": pool_id}
    if kind != "llm":
        raise AgentIdentityValidationError(f"{label}.kind must be llm or pool")
    keys = set(value)
    if not keys.issubset({"kind", "provider", "model", "effort"}):
        raise AgentIdentityValidationError(f"{label} has unsupported fields")
    provider = _text(value.get("provider"), f"{label}.provider", max_length=160).casefold()
    model = _text(value.get("model"), f"{label}.model", max_length=512)
    if not provider:
        raise AgentIdentityValidationError(f"{label}.provider is required")
    from .agent_model_routing import ROUTABLE_PROVIDERS

    if provider not in ROUTABLE_PROVIDERS:
        raise AgentIdentityValidationError(f"unsupported {label}.provider: {provider}")
    if provider == "muse-remote" and model != "muse-chat":
        raise AgentIdentityValidationError(
            f"{label}.model must be muse-chat for muse-remote"
        )
    if provider == "routing-profile" and model != "free-team":
        raise AgentIdentityValidationError(
            f"{label}.model must be free-team for routing-profile"
        )
    result: dict[str, Any] = {"kind": "llm", "provider": provider, "model": model}
    if "effort" in value:
        effort = _text(value.get("effort"), f"{label}.effort", max_length=80, required=False)
        if effort:
            result["effort"] = effort
    return result


def _model_overrides(value: Any, *, label: str = "model_overrides") -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise AgentIdentityValidationError(f"{label} must be an object")
    if len(value) > len(_MODEL_OVERRIDE_ROLES):
        raise AgentIdentityValidationError(f"{label} has too many roles")
    result: dict[str, Any] = {}
    for raw_role, raw_block in value.items():
        role = _text(raw_role, f"{label} role", max_length=64)
        if role not in _MODEL_OVERRIDE_ROLES:
            raise AgentIdentityValidationError(f"unsupported {label} role: {role}")
        if not isinstance(raw_block, Mapping):
            raise AgentIdentityValidationError(f"{label}.{role} must be an object")
        if set(raw_block) - {"routes", "allow_paid_fallback", "fallback_on"}:
            raise AgentIdentityValidationError(f"{label}.{role} has unsupported fields")
        routes = raw_block.get("routes")
        if not isinstance(routes, (list, tuple)) or not routes or len(routes) > _MAX_LIST_ITEMS:
            raise AgentIdentityValidationError(f"{label}.{role}.routes must be a non-empty list")
        block: dict[str, Any] = {
            "routes": [
                _strict_route(route, label=f"{label}.{role}.routes[{index}]")
                for index, route in enumerate(routes)
            ]
        }
        if "allow_paid_fallback" in raw_block:
            if not isinstance(raw_block["allow_paid_fallback"], bool):
                raise AgentIdentityValidationError(
                    f"{label}.{role}.allow_paid_fallback must be boolean"
                )
            block["allow_paid_fallback"] = raw_block["allow_paid_fallback"]
        from .agent_route_fallback_policy import normalize_fallback_on

        try:
            fallback_on = normalize_fallback_on(raw_block.get("fallback_on"))
        except ValueError as exc:
            raise AgentIdentityValidationError(f"{label}.{role}.fallback_on: {exc}") from exc
        if fallback_on is not None:
            block["fallback_on"] = list(fallback_on)
        result[role] = block
    return result


def _tool_ids(
    value: Any,
    *,
    label: str = "tool_ids",
    allow_concrete: bool = False,
) -> list[str]:
    result = _string_list(
        value,
        label=label,
        max_length=255,
        max_items=512 if allow_concrete else _MAX_LIST_ITEMS,
    )
    valid = set(AGENT_CAPABILITY_CATALOG)
    valid.update({"browser", "delegate", "delegate_plan", "delegate_media", "chat_default"})
    valid.update(f"delegate_worker:{kind}" for kind in _DELEGATE_WORKER_KINDS)
    if allow_concrete:
        from ..llm.tool_policy import _AGENT_REVISION_TOOL_GROUPS

        valid.update(_AGENT_REVISION_TOOL_GROUPS)
    unknown = [
        item
        for item in result
        if item not in valid and not (allow_concrete and _ID_RE.fullmatch(item))
    ]
    if unknown:
        raise AgentIdentityValidationError(f"unknown {label} value: {unknown[0]}")
    return result


def _delegate_policy(value: Any, *, label: str = "delegate_policy") -> dict[str, Any]:
    if value in (None, ""):
        return {"agents": [], "workers": []}
    if not isinstance(value, Mapping) or set(value) != {"agents", "workers"}:
        raise AgentIdentityValidationError(
            f"{label} must contain exactly agents and workers"
        )
    agents = value["agents"]
    if agents != "any_owned":
        agents = _string_list(agents, label=f"{label}.agents", max_length=160)
    workers = _string_list(value["workers"], label=f"{label}.workers", max_length=32)
    unknown_workers = [item for item in workers if item not in _DELEGATE_WORKER_KINDS]
    if unknown_workers:
        raise AgentIdentityValidationError(
            f"unknown {label}.workers value: {unknown_workers[0]}"
        )
    return {"agents": agents, "workers": workers}


def _capability_list(value: Any, *, label: str = "capability_ceiling") -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        # A map is accepted only as a strict boolean allow-list convenience.
        if any(not isinstance(item, bool) for item in value.values()):
            raise AgentIdentityValidationError(f"{label} values must be boolean")
        value = [key for key, enabled in value.items() if enabled]
    if not isinstance(value, (list, tuple, set)):
        raise AgentIdentityValidationError(f"{label} must be a list")
    if len(value) > _MAX_LIST_ITEMS:
        raise AgentIdentityValidationError(f"{label} has too many entries")
    result: list[str] = []
    iterable = sorted(value) if isinstance(value, (set, frozenset)) else value
    for item in iterable:
        capability = _text(item, label, max_length=80)
        if capability not in AGENT_CAPABILITY_CATALOG:
            raise AgentIdentityValidationError(f"unknown capability: {capability}")
        if capability not in result:
            result.append(capability)
    return result


def _string_list(
    value: Any,
    *,
    label: str,
    max_length: int = 100,
    allowed: set[str] | None = None,
    max_items: int = _MAX_LIST_ITEMS,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise AgentIdentityValidationError(f"{label} must be a list")
    if len(value) > max_items:
        raise AgentIdentityValidationError(f"{label} has too many entries")
    result: list[str] = []
    iterable = sorted(value) if isinstance(value, (set, frozenset)) else value
    for item in iterable:
        text = _text(item, label, max_length=max_length)
        if allowed is not None and text not in allowed:
            raise AgentIdentityValidationError(f"unknown {label} value: {text}")
        if text not in result:
            result.append(text)
    return result


def _when(value: Any, label: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.utcoffset() is not None else parsed
    except (TypeError, ValueError) as exc:
        raise AgentIdentityValidationError(f"invalid {label}") from exc


def _check_dates(active_from: datetime | None, active_until: datetime | None) -> None:
    if active_from and active_until and active_until < active_from:
        raise AgentIdentityValidationError("active_until must not precede active_from")


class OrganizationService:
    """Transaction-safe singleton bootstrap and settings service."""

    _POLICY_KEYS = {
        "allow_agent_runtime",
        "allow_external_actions",
        "allowed_capabilities",
        "project_permissions",
        "space_permissions",
        "persona_roles",
        "require_human_approval",
        "max_concurrent_runs",
    }
    _BUDGET_KEYS = {"max_daily_cost_micros", "max_run_cost_micros", "currency", "reservation_mode"}

    def __init__(self, db_manager: Any | None = None) -> None:
        self._db_manager = db_manager

    async def _session(self):
        manager = self._db_manager or get_database_manager()
        return await manager.get_session()

    @classmethod
    def normalize_settings(cls, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = payload if isinstance(payload, Mapping) else {}
        display_name = _text(payload.get("display_name", "AoiTalk"), "display_name", max_length=200)
        legal_name = _text(payload.get("legal_name"), "legal_name", max_length=200, required=False) or None
        locale = _text(payload.get("locale", "ja-JP"), "locale", max_length=32)
        timezone = _text(payload.get("timezone", "Asia/Tokyo"), "timezone", max_length=64)
        autonomy_level = _text(payload.get("autonomy_level", "disabled"), "autonomy_level", max_length=16)
        if autonomy_level not in ("disabled", "supervised", "bounded", "autonomous"):
            raise AgentIdentityValidationError("invalid autonomy_level")
        policy = _safe_policy(payload.get("policy"), label="policy", allowed_keys=cls._POLICY_KEYS)
        budget = _safe_policy(payload.get("budget_policy"), label="budget_policy", allowed_keys=cls._BUDGET_KEYS)
        for key in ("allow_agent_runtime", "allow_external_actions", "require_human_approval"):
            if key in policy and not isinstance(policy[key], bool):
                raise AgentIdentityValidationError(f"policy.{key} must be boolean")
        if "max_concurrent_runs" in policy and not isinstance(policy["max_concurrent_runs"], int):
            raise AgentIdentityValidationError("policy.max_concurrent_runs must be an integer")
        for key in ("max_daily_cost_micros", "max_run_cost_micros"):
            if key in budget and not isinstance(budget[key], int):
                raise AgentIdentityValidationError(f"budget_policy.{key} must be an integer")
        if "allowed_capabilities" in policy:
            policy["allowed_capabilities"] = _capability_list(policy["allowed_capabilities"], label="policy.allowed_capabilities")
        for key in ("project_permissions", "space_permissions"):
            if key in policy:
                policy[key] = _string_list(policy[key], label=f"policy.{key}", max_length=64, allowed=set(PROJECT_PERMISSION_KEYS))
        if "persona_roles" in policy:
            policy["persona_roles"] = _string_list(policy["persona_roles"], label="policy.persona_roles", max_length=32)
        return {
            "display_name": display_name,
            "legal_name": legal_name,
            "locale": locale,
            "timezone": timezone,
            "autonomy_level": autonomy_level,
            "policy": policy,
            "budget_policy": budget,
        }

    async def get(self, *, bootstrap: bool = True) -> dict[str, Any] | None:
        if bootstrap:
            return await self.bootstrap()
        session = await self._session()
        try:
            row = (await session.execute(select(Organization).where(Organization.singleton_key == "installation"))).scalar_one_or_none()
            return row.to_safe_dict() if row else None
        finally:
            await session.close()

    async def bootstrap(self, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        normalized = self.normalize_settings(settings) if settings is not None else None
        # A small retry loop handles SQLite's file-level writer lock while the
        # unique singleton constraint handles cross-process races on Postgres.
        for attempt in range(4):
            session = await self._session()
            try:
                row = (await session.execute(select(Organization).where(Organization.singleton_key == "installation"))).scalar_one_or_none()
                if row is not None:
                    # Capture before closing/rolling back; rollback may expire
                    # ORM attributes when a caller uses the default session
                    # configuration.
                    payload = row.to_safe_dict()
                    await session.rollback()
                    return payload
                values = normalized or self.normalize_settings(None)
                row = Organization(
                    id=uuid4(), singleton_key="installation", display_name=values["display_name"],
                    legal_name=values["legal_name"], locale=values["locale"], timezone=values["timezone"],
                    autonomy_level=values["autonomy_level"], policy_json=values["policy"],
                    budget_policy_json=values["budget_policy"], policy_version=1,
                )
                session.add(row)
                await session.flush()
                payload = row.to_safe_dict()
                await session.commit()
                return payload
            except IntegrityError:
                await session.rollback()
                # Another transaction won the fixed-key race.
                winner = (await session.execute(select(Organization).where(Organization.singleton_key == "installation"))).scalar_one_or_none()
                if winner is not None:
                    return winner.to_safe_dict()
                if attempt >= 3:
                    raise AgentIdentityConflictError("organization bootstrap conflicted")
                await asyncio.sleep(0.01 * (attempt + 1))
            except OperationalError:
                await session.rollback()
                if attempt >= 3:
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))
            finally:
                await session.close()
        raise AgentIdentityConflictError("organization bootstrap failed")

    async def update(self, payload: Mapping[str, Any], *, actor_user_id: Any) -> dict[str, Any]:
        actor = _uuid(actor_user_id, "actor_user_id")
        session = await self._session()
        try:
            user = await session.get(User, actor)
            if user is None or not bool(user.is_active) or str(user.role or "") != "admin":
                raise AgentIdentityAuthorizationError("administrator authorization required")
            row = (await session.execute(select(Organization).where(Organization.singleton_key == "installation").with_for_update())).scalar_one_or_none()
            if row is None:
                row = Organization(singleton_key="installation")
                session.add(row)
                await session.flush()
            expected_policy_version = payload.get("expected_policy_version")
            if expected_policy_version is not None:
                try:
                    expected_policy_version = int(expected_policy_version)
                except (TypeError, ValueError) as exc:
                    raise AgentIdentityValidationError("invalid expected_policy_version") from exc
                if expected_policy_version != int(row.policy_version or 1):
                    raise AgentIdentityConflictError("organization policy version is stale")
            normalized = self.normalize_settings({
                "display_name": payload.get("display_name", row.display_name),
                "legal_name": payload.get("legal_name", row.legal_name),
                "locale": payload.get("locale", row.locale),
                "timezone": payload.get("timezone", row.timezone),
                "autonomy_level": payload.get("autonomy_level", row.autonomy_level),
                "policy": payload.get("policy", payload.get("policy_json", row.policy_json)),
                "budget_policy": payload.get(
                    "budget_policy", payload.get("budget_policy_json", row.budget_policy_json)
                ),
            })
            for key, value in normalized.items():
                if key == "policy":
                    row.policy_json = value
                elif key == "budget_policy":
                    row.budget_policy_json = value
                else:
                    setattr(row, key, value)
            row.policy_version = int(row.policy_version or 1) + 1
            row.updated_at = datetime.utcnow()
            result = row.to_safe_dict()
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    get_organization = get
    get_or_create = bootstrap
    bootstrap_organization = bootstrap
    bootstrap_singleton = bootstrap
    update_organization = update


class AgentIdentityService:
    """CRUD and relationship service for generic Agents."""

    _WAKE_KEYS = {"mode", "interval_seconds", "jitter_seconds", "max_wakes_per_day"}
    _BUDGET_KEYS = {"max_run_cost_micros", "max_daily_cost_micros", "currency"}
    _CONCURRENCY_KEYS = {"max_parallel_runs", "max_parallel_tasks", "queue_class"}
    _STATE_TRANSITIONS = {
        "draft": {"draft", "active", "retired"},
        "active": {"active", "paused", "retired"},
        "paused": {"paused", "active", "retired"},
        "retired": {"retired"},
    }

    def __init__(self, db_manager: Any | None = None, *, config: Any | None = None) -> None:
        self._db_manager = db_manager
        self.config = config
        self.organizations = OrganizationService(db_manager)

    @staticmethod
    def is_default_chat_agent(agent: Any, *, owner_user_id: Any | None = None) -> bool:
        """Identify the private Chat Agent by its stable owner key.

        The check deliberately ignores ``display_name`` and ``slug``.  A user
        may edit either presentation value while the private identity remains
        tied to the provisioning kind and owner.
        """

        if agent is None:
            return False
        owner = getattr(agent, "created_by", None)
        if owner is None and isinstance(agent, Mapping):
            owner = agent.get("created_by")
        if owner is None:
            return False
        if owner_user_id is not None and str(owner) != str(owner_user_id):
            return False
        key = getattr(agent, "idempotency_key", None)
        if key is None and isinstance(agent, Mapping):
            key = agent.get("idempotency_key")
        return str(key or "") == aoi_default_agent_idempotency_key(owner)

    @classmethod
    def _mark_agent_projection(cls, agent: Any) -> dict[str, Any]:
        """Project an Agent while exposing the stable default-chat marker."""

        payload = agent.to_safe_dict() if hasattr(agent, "to_safe_dict") else dict(agent)
        payload["is_default_chat_agent"] = cls.is_default_chat_agent(agent)
        return payload

    async def _session(self):
        manager = self._db_manager or get_database_manager()
        return await manager.get_session()

    @staticmethod
    def _require_human(user_id: Any) -> UUID:
        return _uuid(user_id, "actor_user_id")

    async def _assert_admin(self, session, user_id: Any) -> UUID:
        actor = self._require_human(user_id)
        # A typed Agent identity is never accepted as a human grantor even if
        # a malformed deployment happens to reuse the same UUID in ``users``.
        possible_agent = await session.get(Agent, actor)
        if possible_agent is not None and hasattr(possible_agent, "state"):
            raise AgentIdentityAuthorizationError("Agent principals cannot administer identity")
        user = await session.get(User, actor)
        if user is None or not bool(user.is_active) or str(user.role or "") != "admin":
            raise AgentIdentityAuthorizationError("administrator authorization required")
        return actor

    async def _assert_active_human_owner(self, session, owner_user_id: Any) -> UUID:
        """Validate a real active human for owner-only private provisioning.

        Private AoiDots provisioning is intentionally separate from the admin
        CRUD surface.  The owner must still be a database-backed active
        ``User``; a request-shaped role, an Agent UUID, or a synthetic
        service principal cannot satisfy this check.
        """

        owner = self._require_human(owner_user_id)
        possible_agent = await session.get(Agent, owner)
        if possible_agent is not None and hasattr(possible_agent, "state"):
            raise AgentIdentityAuthorizationError("Agent principals cannot own private provisioning")
        user = await session.get(User, owner)
        if user is None or not bool(user.is_active):
            raise AgentIdentityAuthorizationError("an active human owner is required")
        return owner

    async def _assert_agent_editor(self, session, agent: Agent, actor_user_id: Any) -> UUID:
        """Authorize generic admin edits plus the owner's default Chat Agent."""

        actor = self._require_human(actor_user_id)
        possible_agent = await session.get(Agent, actor)
        if possible_agent is not None and hasattr(possible_agent, "state"):
            raise AgentIdentityAuthorizationError("Agent principals cannot edit identity")
        user = await session.get(User, actor)
        if user is None or not bool(user.is_active):
            raise AgentIdentityAuthorizationError("active human authorization required")
        if str(user.role or "") == "admin":
            return actor
        if (
            agent is not None
            and str(agent.created_by or "") == str(actor)
            and self.is_default_chat_agent(agent, owner_user_id=actor)
        ):
            return actor
        raise AgentIdentityAuthorizationError("administrator or default Chat Agent owner authorization required")

    @staticmethod
    def _revision_content_payload(revision: Any) -> dict[str, Any]:
        """Rebuild the canonical hash material for one AgentRevision row."""

        def value(name: str, default: Any = None) -> Any:
            if isinstance(revision, Mapping):
                return revision.get(name, default)
            return getattr(revision, name, default)

        def mapping_value(primary: str, alias: str) -> dict[str, Any]:
            raw = value(primary, value(alias, {}))
            return dict(raw) if isinstance(raw, Mapping) else {}

        payload = {
            "agent_id": str(value("agent_id") or ""),
            "display_name": str(value("display_name") or ""),
            "mission": str(value("mission") or ""),
            "responsibility_summary": str(value("responsibility_summary") or ""),
            "operational_instructions": str(value("operational_instructions") or ""),
            "agent_team_id": str(value("agent_team_id") or ""),
            "execution_profile_id": str(value("execution_profile_id") or ""),
            "allowed_subagent_ids": list(value("allowed_subagent_ids") or []),
            "capability_ceiling": list(
                value("capability_ceiling_json", value("capability_ceiling", [])) or []
            ),
            "wake_policy": mapping_value("wake_policy_json", "wake_policy"),
            "budget_policy": mapping_value("budget_policy_json", "budget_policy"),
            "concurrency_policy": mapping_value(
                "concurrency_policy_json", "concurrency_policy"
            ),
        }
        # P2 is expand-only.  Omit absent/NULL fields so legacy revisions keep
        # their old hashes; once a non-NULL value is persisted, it becomes part
        # of the immutable content contract for subsequent revisions.
        for name, default in (
            ("model_overrides", {}),
            ("tool_ids", []),
            ("delegate_policy", {}),
        ):
            optional = _optional_revision_value(revision, name, _OMITTED)
            if optional is _OMITTED or optional is None:
                continue
            if name == "tool_ids":
                payload[name] = list(optional or [])
            elif isinstance(optional, Mapping):
                payload[name] = dict(optional)
            else:
                payload[name] = default
        return payload

    @classmethod
    def _revision_content_hash(cls, revision: Any) -> str:
        return sha256_json(cls._revision_content_payload(revision))

    @classmethod
    def _revision_content_matches(cls, revision: Any, content: Mapping[str, Any]) -> bool:
        """Accept a pre-P2 idempotent row whose hash has no nullable fields."""

        actual = str(getattr(revision, "content_hash", "") or "").strip().lower()
        current = cls._revision_content_hash(content)
        if actual == current:
            return True
        if any(getattr(revision, name, None) is not None for name in (
            "model_overrides", "tool_ids", "delegate_policy"
        )):
            return False
        legacy = {
            key: value
            for key, value in content.items()
            if key not in {"model_overrides", "tool_ids", "delegate_policy"}
        }
        return actual == cls._revision_content_hash(legacy)

    @classmethod
    def _assert_revision_content_hash(cls, revision: Any) -> str:
        expected = cls._revision_content_hash(revision)
        actual = str(
            revision.get("content_hash")
            if isinstance(revision, Mapping)
            else getattr(revision, "content_hash", "")
        ).strip().lower()
        if actual != expected:
            raise AgentIdentityConflictError("Agent revision content hash is invalid")
        return expected

    def _assert_private_general_worker_route(
        self,
        revision: Any,
        *,
        config: Any | None = None,
    ) -> None:
        """Require the pure catalog/profile preflight for ``general_worker``.

        Private bootstrap must not import provider SDKs, Spotify tools, or the
        full ToolRegistry merely to decide whether a pinned Team/Profile/
        Subagent declaration is structurally executable.  ``manual`` is the
        explicit Main-inherit profile; it is accepted only after Team
        membership, enabled Subagent state, and capability intersection are
        validated.  Runtime/provider construction remains a later gate.
        """

        def read(name: str, default: Any = None) -> Any:
            if isinstance(revision, Mapping):
                return revision.get(name, default)
            return getattr(revision, name, default)

        route_config = config if config is not None else self.config
        if route_config is None:
            try:
                from ..config_defaults import load_default_config

                route_config = load_default_config()
            except Exception:
                route_config = {}

        # P5 revisions have no executable Team/Profile/Subagent topology.
        # Validate the canonical v2 chat role directly and keep the old graph
        # checks below only for historical rows that are still being read.
        from .agent_revision_v2 import revision_v2_fields_present, resolve_agent_role_route_v2

        if revision_v2_fields_present(revision):
            try:
                route, _source = resolve_agent_role_route_v2(
                    route_config,
                    revision,
                    "chat",
                )
            except Exception as exc:
                raise AgentIdentityConflictError(
                    "private Agent chat route is unavailable"
                ) from exc
            if not isinstance(route, Mapping):
                raise AgentIdentityConflictError("private Agent chat route is unavailable")
            return

        raise AgentIdentityConflictError("private Agent v2 chat route is unavailable")

    async def _create_private_agent_and_revision(
        self,
        session,
        *,
        owner_user_id: Any,
        purpose: Any = "aoidots",
        config: Any | None = None,
        agent_id: Any | None = None,
        revision_id: Any | None = None,
        private_role: Any = "root",
        agent_idempotency_key: Any | None = None,
        revision_idempotency_key: Any | None = None,
        display_name: Any | None = None,
        mission: Any | None = None,
        responsibility_summary: Any | None = None,
        operational_instructions: Any | None = None,
        capability_ceiling: Any | None = None,
        tool_ids: Any | None = None,
        model_overrides: Any | None = None,
        delegate_policy: Any | None = None,
    ) -> tuple[Agent, AgentRevision]:
        """Create the ordinary Agent identity used by a private root.

        This is an internal transaction helper for
        :class:`AgentPrivateProvisioningService`.  It deliberately does not
        accept an actor role, provider/model, or scheduler. The root uses the
        v2 role table and a small neutral capability catalog intersection that
        is safe for the private root. The internal ``chat_default`` role may
        supply the already-resolved Chat capability and tool projection.
        """

        owner = await self._assert_active_human_owner(session, owner_user_id)
        purpose_text = _text(purpose, "purpose", max_length=64)
        if purpose_text != "aoidots":
            raise AgentIdentityValidationError("private provisioning purpose must be aoidots")
        role_text = _text(private_role, "private_role", max_length=32)
        if role_text not in {"root", "general_worker", "chat_default"}:
            raise AgentIdentityValidationError("unsupported private Agent role")

        # These are the only private-root settings that can be derived from
        # normal Agent configuration.  In particular, callers cannot widen
        # the capability ceiling or select a provider/model here.
        supplied = config if isinstance(config, Mapping) else {}
        default_name = (
            "AoiDots"
            if role_text == "root"
            else "AoiDots General Worker"
            if role_text == "general_worker"
            else "Aoi"
        )
        display_name = _text(
            (
                supplied.get("display_name", default_name)
                if role_text == "root"
                else display_name if display_name is not None else default_name
            ),
            "display_name",
            max_length=160,
        )
        if capability_ceiling is None:
            capability_ceiling = [
                capability
                for capability in ("aoi_tools", "web_read", "workspace_read")
                if capability in AGENT_CAPABILITY_CATALOG
            ]
        else:
            capability_ceiling = _capability_list(capability_ceiling)
        ceiling = _capability_list(capability_ceiling)
        owner_text = str(owner)
        agent_key = _idempotency(
            agent_idempotency_key
            or (
                f"{purpose_text}:agent:{owner_text}"
                if role_text == "root"
                else aoi_default_agent_idempotency_key(owner)
                if role_text == "chat_default"
                else f"private-worker:{owner_text}:general:v1"
            ),
            "private_agent_idempotency_key",
        )
        revision_key = _idempotency(
            revision_idempotency_key
            or (
                f"{purpose_text}:revision:{owner_text}:1"
                if role_text == "root"
                else f"{aoi_default_agent_idempotency_key(owner)}:revision"
                if role_text == "chat_default"
                else f"private-worker:{owner_text}:general:v1:revision"
            ),
            "private_revision_idempotency_key",
        )
        mission = (
            "" if role_text == "root"
            else mission if mission is not None
            else "Provide bounded general-purpose assistance for the owner."
        )
        responsibility = (
            "" if role_text == "root"
            else responsibility_summary
            if responsibility_summary is not None
            else "Owner-scoped general worker for AoiDots jobs."
        )
        operational_instructions = (
            "" if role_text == "root"
            else operational_instructions if operational_instructions is not None else ""
        )
        mission = _text(mission, "mission", max_length=10_000, required=False)
        responsibility = _text(responsibility, "responsibility_summary", max_length=10_000, required=False)
        operational_instructions = _text(
            operational_instructions,
            "operational_instructions",
            max_length=30_000,
            required=False,
        )
        concurrency_policy = (
            {"max_parallel_runs": 1, "max_parallel_tasks": 1}
            if role_text in {"root", "chat_default"}
            else {"max_parallel_runs": 2, "max_parallel_tasks": 2}
        )
        if role_text == "chat_default":
            model_overrides = _model_overrides(model_overrides or {})
            tool_ids = (
                _tool_ids(tool_ids, allow_concrete=True)
                if tool_ids is not None
                else None
            )
            delegate_policy = _delegate_policy(
                delegate_policy
                if delegate_policy is not None
                else {"agents": "any_owned", "workers": []}
            )
        else:
            model_overrides = {}
            tool_ids = list(ceiling)
            delegate_policy = {"agents": [], "workers": []}
        create_hash = sha256_json(
            {"display_name": display_name, "slug": None, "character_id": None}
        )
        expected_agent_id = _uuid(agent_id, "agent_id", required=False)
        expected_revision_id = _uuid(revision_id, "agent_revision_id", required=False)

        created_agent = False
        agent = (
            await session.execute(
                select(Agent).where(Agent.idempotency_key == agent_key).with_for_update()
            )
        ).scalar_one_or_none()
        if agent is None:
            agent = Agent(
                id=expected_agent_id or uuid4(),
                display_name=display_name,
                slug=None,
                state="draft",
                character_id=None,
                create_hash=create_hash,
                idempotency_key=agent_key,
                created_by=owner,
            )
            session.add(agent)
            await session.flush()
            created_agent = True
        elif role_text == "chat_default":
            # Aoi is identified by the owner-scoped kind/key and stable UUID.
            # Its display label and create hash are mutable presentation
            # metadata edited through Operations, so they must not make a
            # replay conflict. State is still fail-closed below.
            if (
                agent.created_by != owner
                or (expected_agent_id is not None and agent.id != expected_agent_id)
            ):
                raise AgentIdentityConflictError("private Agent identity is owned by another configuration")
        elif (
            agent.create_hash != create_hash
            or agent.created_by != owner
            or agent.display_name != display_name
            or (expected_agent_id is not None and agent.id != expected_agent_id)
        ):
            raise AgentIdentityConflictError("private Agent identity is owned by another configuration")

        if created_agent and role_text == "chat_default" and tool_ids is None:
            raise AgentIdentityValidationError(
                "default Chat tool_ids must be derived from the Chat catalog"
            )

        if not created_agent:
            # A private root is allowed to activate the identity it created in
            # this transaction. A later bootstrap must never reactivate a
            # paused/retired row or overwrite a pre-existing Agent setup.
            if str(agent.state or "") != "active":
                raise AgentIdentityConflictError("private Agent identity is inactive")

        revision = (
            await session.execute(
                select(AgentRevision)
                .where(
                    AgentRevision.agent_id == agent.id,
                    AgentRevision.idempotency_key == revision_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None:
            if not created_agent:
                raise AgentIdentityConflictError("private Agent already has no canonical revision")
            latest = (
                await session.execute(
                    select(func.max(AgentRevision.version)).where(AgentRevision.agent_id == agent.id)
                )
            ).scalar_one()
            version = int(latest or 0) + 1
            if version != 1:
                raise AgentIdentityConflictError("private Agent has an unexpected existing revision")
            revision = AgentRevision(
                id=expected_revision_id or uuid4(),
                agent_id=agent.id,
                version=version,
                display_name=display_name,
                mission=mission,
                responsibility_summary=responsibility,
                operational_instructions=operational_instructions,
                agent_team_id=None,
                execution_profile_id=None,
                allowed_subagent_ids=None,
                capability_ceiling_json=ceiling,
                wake_policy_json={"mode": "manual"},
                budget_policy_json={},
                concurrency_policy_json=concurrency_policy,
                # Filled after optional P2 fields are attached below.
                content_hash="0" * 64,
                idempotency_key=revision_key,
                created_by=owner,
            )
            _set_optional_revision_value(revision, "model_overrides", model_overrides)
            _set_optional_revision_value(revision, "tool_ids", tool_ids)
            _set_optional_revision_value(revision, "delegate_policy", delegate_policy)
            digest = self._revision_content_hash(revision)
            revision.content_hash = digest
            session.add(revision)
            await session.flush()
        elif (
            revision.agent_id != agent.id
            or revision.created_by != owner
            or (expected_revision_id is not None and revision.id != expected_revision_id)
        ):
            raise AgentIdentityConflictError("private Agent revision is owned by another configuration")
        else:
            # The canonical revision is immutable.  Replays must validate the
            # stored content, while later Chat configuration changes are
            # represented by a new revision rather than conflicting with this
            # owner-stable v1 identity row.
            digest = self._revision_content_hash(revision)
            if revision.content_hash != digest:
                raise AgentIdentityConflictError("private Agent revision content does not match its identity")
        if revision is not None and (
            revision.content_hash != self._revision_content_hash(revision)
            or revision.agent_id != agent.id
            or revision.created_by != owner
            or (expected_revision_id is not None and revision.id != expected_revision_id)
        ):
            raise AgentIdentityConflictError("private Agent revision is owned by another configuration")

        self._assert_revision_content_hash(revision)

        # A legacy Aoi revision is upgraded immediately below by the Chat
        # provisioning path. Let that historical source row pass through the
        # identity helper once; every newly exposed revision is v2 checked.
        from .agent_revision_v2 import revision_v2_fields_present

        if role_text != "chat_default" or revision_v2_fields_present(revision):
            self._assert_private_general_worker_route(revision, config=config)

        if created_agent:
            agent.state = "active"
            agent.updated_at = datetime.utcnow()
        return agent, revision

    def _revision_route_warnings(self, revision: Mapping[str, Any]) -> list[dict[str, str]]:
        """Keep historical route warnings out of live revision execution."""

        del revision
        return []

    def _revision_v2_fields(
        self,
        *,
        previous: Any | None,
        model_overrides: Any,
        tool_ids: Any,
        delegate_policy: Any,
        capability_ceiling: list[str],
        derived_defaults: tuple[dict[str, Any], list[str], dict[str, Any]] | None = None,
        allow_concrete_tool_ids: bool = False,
    ) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        """Normalize P2 fields, inheriting a prior revision when omitted."""

        def previous_value(name: str) -> Any:
            if previous is None:
                return None
            return getattr(previous, name, None)

        inherited_model = previous_value("model_overrides")
        inherited_tools = previous_value("tool_ids")
        inherited_delegate = previous_value("delegate_policy")
        default_model, default_tools, default_delegate = derived_defaults or (
            {},
            list(capability_ceiling),
            {"agents": [], "workers": []},
        )
        if model_overrides is _P2_UNSET or model_overrides is None:
            model_overrides = inherited_model if inherited_model is not None else default_model
        if tool_ids is _P2_UNSET or tool_ids is None:
            tool_ids = inherited_tools if inherited_tools is not None else default_tools
        if delegate_policy is _P2_UNSET or delegate_policy is None:
            delegate_policy = inherited_delegate if inherited_delegate is not None else default_delegate
        return (
            _model_overrides(model_overrides),
            _tool_ids(tool_ids, allow_concrete=allow_concrete_tool_ids),
            _delegate_policy(delegate_policy),
        )

    def _effective_model_routes(self, revision: Mapping[str, Any]) -> dict[str, Any]:
        """Project effective role routes through the optional P2 resolver.

        The follow-up resolver contract is intentionally small:
        ``resolve_agent_role_route_v2(config, revision, role)`` returns
        ``(route_dict_or_none, source)``.  The API stores that exact pair as
        ``{"route": ..., "source": ...}`` and omits unavailable roles.
        """

        try:
            from .agent_revision_v2 import resolve_agent_role_route_v2
        except (ImportError, AttributeError):
            return {}
        routes: dict[str, Any] = {}
        for role in _MODEL_ROLES:
            try:
                resolved = resolve_agent_role_route_v2(self.config, revision, role)
            except Exception:
                # Route availability is diagnostic data at this boundary;
                # malformed or unavailable roles are reported by the v2
                # resolver and must not make revision reads fail closed.
                logger.debug("P2 route unavailable for %s", role, exc_info=True)
                continue
            if isinstance(resolved, tuple) and len(resolved) == 2:
                route, source = resolved
            else:
                # Keep compatibility with the early development helper while
                # the shared tree converges on the tuple contract.
                route, source = resolved, "unknown"
            if route is None or not isinstance(route, Mapping):
                continue
            routes[role] = {
                "route": _bounded_json_value(route, label=f"effective_model_routes.{role}.route"),
                "source": _text(source, f"effective_model_routes.{role}.source", max_length=64),
            }
        return routes

    def _revision_projection(self, revision: Any) -> dict[str, Any]:
        payload = revision.to_safe_dict() if hasattr(revision, "to_safe_dict") else dict(revision)
        warnings = self._revision_route_warnings(payload)
        if warnings:
            payload["route_warnings"] = warnings
        payload["effective_model_routes"] = self._effective_model_routes(payload)
        return payload

    def agent_tools_catalog(self) -> dict[str, Any]:
        """Return the bounded Japanese Agent tool/capability catalog."""

        tools = []
        for capability_id, metadata in AGENT_CAPABILITY_CATALOG.items():
            tools.append(
                {
                    "id": capability_id,
                    "label": _AGENT_TOOL_LABELS.get(capability_id, capability_id),
                    "group": str(metadata.get("family") or "capability"),
                    "description": _AGENT_TOOL_DESCRIPTIONS.get(
                        capability_id, "この機能を Agent に公開します。"
                    ),
                    "requires_capabilities": [capability_id],
                }
            )
        tools.extend(
            {
                "id": item["id"],
                "label": item["label"],
                "group": {"browser": "browser", "chat_default": "aoi", "delegate_media": "aoi_tools"}.get(item["id"], "delegation"),
                "description": item["description"],
                "requires_capabilities": list(item["capability_ids"]),
            }
            for item in _AGENT_TOOL_GROUPS
        )
        return {"tools": tools}

    async def employee_catalog(self, *, actor_user_id: Any) -> dict[str, Any]:
        """Return the neutral capability catalog for the Operations editor."""

        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
        finally:
            await session.close()
        return {"teams": [], "capabilities": list(AGENT_CAPABILITY_CATALOG.values()),
                "can_manage": True, "runtime_profile": Features.profile_name()}

    async def create_agent(
        self,
        *,
        display_name: Any,
        idempotency_key: Any,
        actor_user_id: Any | None = None,
        created_by_user_id: Any | None = None,
        created_by: Any | None = None,
        slug: Any | None = None,
        character_id: Any | None = None,
    ) -> dict[str, Any]:
        name = _text(display_name, "display_name", max_length=160)
        key = _idempotency(idempotency_key)
        normalized_slug = _text(slug, "slug", max_length=100, required=False).lower() or None
        if normalized_slug and not _SLUG_RE.fullmatch(normalized_slug):
            raise AgentIdentityValidationError("invalid slug")
        character = _uuid(character_id, "character_id", required=False)
        creator = _uuid(
            actor_user_id if actor_user_id is not None else (
                created_by_user_id if created_by_user_id is not None else created_by
            ),
            "actor_user_id",
            required=False,
        )
        create_hash = sha256_json({"display_name": name, "slug": normalized_slug, "character_id": str(character) if character else None})
        session = await self._session()
        try:
            await self._assert_admin(session, creator)
            existing = (await session.execute(select(Agent).where(Agent.idempotency_key == key))).scalar_one_or_none()
            if existing is not None:
                if existing.create_hash != create_hash:
                    raise AgentIdentityConflictError("idempotency key was used with different Agent content")
                return self._mark_agent_projection(existing)
            if character is not None:
                try:
                    from ..models.ecc_models import Character

                    character_row = await session.get(Character, character)
                except Exception as exc:
                    raise AgentIdentityNotFoundError("Character not found") from exc
                if character_row is None:
                    raise AgentIdentityNotFoundError("Character not found")
            row = Agent(
                id=uuid4(), display_name=name, slug=normalized_slug, character_id=character,
                create_hash=create_hash, idempotency_key=key, created_by=creator, state="draft",
            )
            session.add(row)
            await session.flush()
            payload = self._mark_agent_projection(row)
            await session.commit()
            return payload
        except IntegrityError:
            await session.rollback()
            winner = (await session.execute(select(Agent).where(Agent.idempotency_key == key))).scalar_one_or_none()
            if winner is not None and winner.create_hash == create_hash:
                return self._mark_agent_projection(winner)
            raise AgentIdentityConflictError("Agent creation conflicted")
        finally:
            await session.close()

    async def list_agents(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
        owner_user_id: Any | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit or 100), 1), 200)
        owner = _uuid(owner_user_id, "owner_user_id", required=False)
        session = await self._session()
        try:
            stmt = select(Agent).order_by(Agent.created_at.desc()).limit(safe_limit)
            if state:
                if state not in {"draft", "active", "paused", "retired"}:
                    raise AgentIdentityValidationError("invalid agent state")
                stmt = stmt.where(Agent.state == state)
            if owner is not None:
                stmt = stmt.where(Agent.created_by == owner)
            return [self._mark_agent_projection(item) for item in (await session.execute(stmt)).scalars().all()]
        finally:
            await session.close()

    async def get_agent(self, agent_id: Any) -> dict[str, Any] | None:
        parsed = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            row = await session.get(Agent, parsed)
            return self._mark_agent_projection(row) if row else None
        finally:
            await session.close()

    async def update_thumbnail(self, *, agent_id: Any, artifact_id: Any | None, expected_artifact_id: Any | None, actor_user_id: Any) -> dict[str, Any]:
        """Select an owned, verified image without changing Agent authority or Persona links."""
        from io import BytesIO
        from PIL import Image, UnidentifiedImageError
        from .operations_service import OperationsError, OperationsService

        aid = _uuid(agent_id, "agent_id")
        image_id = _uuid(artifact_id, "artifact_id", required=False)
        expected = _uuid(expected_artifact_id, "expected_artifact_id", required=False)
        session = await self._session()
        try:
            actor = await self._assert_admin(session, actor_user_id)
            row = (await session.execute(select(Agent).where(Agent.id == aid).with_for_update())).scalar_one_or_none()
            if row is None:
                raise AgentIdentityNotFoundError("Agent not found")
            if row.thumbnail_artifact_id != expected:
                raise AgentIdentityConflictError("Agent thumbnail has changed")
            if image_id is not None:
                try:
                    artifact, content = await OperationsService().get_artifact_content(session, {"id": actor, "actor_type": "human"}, image_id)
                except OperationsError as exc:
                    raise AgentIdentityError("image artifact is unavailable", status_code=exc.status_code) from exc
                if artifact["owner_user_id"] != str(actor):
                    raise AgentIdentityAuthorizationError("image artifact must be owned by the administrator")
                formats = {"image/png": "PNG", "image/jpeg": "JPEG"}
                if artifact["mime_type"] not in formats:
                    raise AgentIdentityValidationError("thumbnail must be a PNG or JPEG image")
                try:
                    with Image.open(BytesIO(content)) as image:
                        if image.format != formats[artifact["mime_type"]] or image.width * image.height > 32_000_000:
                            raise AgentIdentityValidationError("thumbnail image format or dimensions are invalid")
                        image.verify()
                except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                    raise AgentIdentityValidationError("thumbnail image is invalid") from exc
            row.thumbnail_artifact_id = image_id
            row.updated_at = datetime.utcnow()
            await session.commit()
            return {"agent_id": str(aid), "artifact_id": str(image_id) if image_id else None}
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    create = create_agent
    list = list_agents
    get = get_agent

    async def transition_agent(self, agent_id: Any, state: Any, *, actor_user_id: Any, expected_state: str | None = None) -> dict[str, Any]:
        target = _text(state, "state", max_length=16)
        if target not in {"draft", "active", "paused", "retired"}:
            raise AgentIdentityValidationError("invalid agent state")
        session = await self._session()
        try:
            row = await session.get(Agent, _uuid(agent_id, "agent_id"))
            if row is None:
                raise AgentIdentityNotFoundError("Agent not found")
            await self._assert_agent_editor(session, row, actor_user_id)
            if self.is_default_chat_agent(row) and target != "active":
                raise AgentIdentityConflictError("default Chat Agent cannot be paused or retired")
            if target not in self._STATE_TRANSITIONS.get(str(row.state), set()):
                raise AgentIdentityConflictError(f"invalid Agent state transition: {row.state} -> {target}")
            if expected_state is not None and expected_state != row.state:
                raise AgentIdentityConflictError("Agent state changed; reload before saving")
            if target == "active":
                revision = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == row.id).order_by(AgentRevision.version.desc()).limit(1))).scalar_one_or_none()
                if revision is None:
                    raise AgentIdentityConflictError("Agent revision is required before activation")
                self._assert_revision_content_hash(revision)
                # Activation only admits a complete P2 revision. The retired
                # Team/Profile/Subagent columns remain readable on history
                # rows, but are not required for a current Agent identity.
                from .agent_revision_v2 import (
                    revision_delegate_policy,
                    revision_model_overrides,
                    revision_tool_ids,
                )

                if any(
                    value is None
                    for value in (
                        revision_model_overrides(revision),
                        revision_tool_ids(revision),
                        revision_delegate_policy(revision),
                    )
                ):
                    raise AgentIdentityConflictError(
                        "Agent v2 revision is required before activation"
                    )
                _model_overrides(revision_model_overrides(revision))
                _tool_ids(revision_tool_ids(revision), allow_concrete=True)
                _delegate_policy(revision_delegate_policy(revision))
                _capability_list(revision.capability_ceiling_json)
            changed = await session.execute(update(Agent).where(Agent.id == row.id, Agent.state == row.state)
                .values(state=target, updated_at=datetime.utcnow()))
            if changed.rowcount != 1:
                raise AgentIdentityConflictError("Agent state changed; reload before saving")
            await session.commit()
            await session.refresh(row)
            return self._mark_agent_projection(row)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    transition_state = transition_agent

    async def create_revision(
        self,
        *,
        agent_id: Any,
        display_name: Any = _OMITTED,
        mission: Any = _OMITTED,
        responsibility_summary: Any = _OMITTED,
        operational_instructions: Any = _OMITTED,
        agent_team_id: Any = _OMITTED,
        execution_profile_id: Any = _OMITTED,
        allowed_subagent_ids: Any = _OMITTED,
        capability_ceiling: Any = _OMITTED,
        wake_policy: Any = _OMITTED,
        budget_policy: Any = _OMITTED,
        concurrency_policy: Any = _OMITTED,
        model_overrides: Any = _P2_UNSET,
        tool_ids: Any = _P2_UNSET,
        delegate_policy: Any = _P2_UNSET,
        idempotency_key: Any = _OMITTED,
        actor_user_id: Any | None = None,
        created_by_user_id: Any | None = None,
        created_by: Any | None = None,
        version: int | None = None,
        content_hash: Any | None = None,
    ) -> dict[str, Any]:
        aid = _uuid(agent_id, "agent_id")
        key = _idempotency(idempotency_key)
        creator = _uuid(
            actor_user_id if actor_user_id is not None else (
                created_by_user_id if created_by_user_id is not None else created_by
            ),
            "actor_user_id",
            required=False,
        )
        session = await self._session()
        try:
            agent = await session.get(Agent, aid)
            if agent is None:
                raise AgentIdentityNotFoundError("Agent not found")
            await self._assert_agent_editor(session, agent, creator)
            existing = (
                await session.execute(
                    select(AgentRevision).where(
                        AgentRevision.agent_id == aid,
                        AgentRevision.idempotency_key == key,
                    )
                )
            ).scalar_one_or_none()
            latest = existing
            if latest is None:
                latest = (
                    await session.execute(
                        select(AgentRevision)
                        .where(AgentRevision.agent_id == aid)
                        .order_by(AgentRevision.version.desc(), AgentRevision.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

            def inherit(value: Any, attr: str, default: Any) -> Any:
                if value is not _OMITTED and value is not None:
                    return value
                if latest is not None:
                    prior = getattr(latest, attr, None)
                    if prior is not None:
                        return prior
                return default

            name = _text(
                inherit(display_name, "display_name", agent.display_name or "Agent"),
                "display_name",
                max_length=160,
            )
            mission_text = _text(
                inherit(mission, "mission", ""), "mission", max_length=10_000, required=False
            )
            responsibility = _text(
                inherit(responsibility_summary, "responsibility_summary", ""),
                "responsibility_summary",
                max_length=10_000,
                required=False,
            )
            instructions = _text(
                inherit(operational_instructions, "operational_instructions", ""),
                "operational_instructions",
                max_length=30_000,
                required=False,
            )
            # The retired topology arguments remain accepted at this
            # internal boundary only so historical callers can be migrated in
            # place. They are never read for live routing, persisted, or
            # included in the new revision hash.
            del agent_team_id, execution_profile_id, allowed_subagent_ids
            ceiling = _capability_list(
                inherit(capability_ceiling, "capability_ceiling_json", [])
            )
            wake = _safe_policy(
                inherit(wake_policy, "wake_policy_json", {}),
                label="wake_policy",
                allowed_keys=self._WAKE_KEYS,
            )
            budget = _safe_policy(
                inherit(budget_policy, "budget_policy_json", {}),
                label="budget_policy",
                allowed_keys=self._BUDGET_KEYS,
            )
            concurrency = _safe_policy(
                inherit(concurrency_policy, "concurrency_policy_json", {}),
                label="concurrency_policy",
                allowed_keys=self._CONCURRENCY_KEYS,
            )
            if "mode" in wake and wake["mode"] not in {"manual", "interval", "event"}:
                raise AgentIdentityValidationError("invalid wake_policy.mode")
            for field in ("interval_seconds", "jitter_seconds", "max_wakes_per_day"):
                if field in wake and not isinstance(wake[field], int):
                    raise AgentIdentityValidationError(f"wake_policy.{field} must be an integer")
            for field in ("max_run_cost_micros", "max_daily_cost_micros"):
                if field in budget and not isinstance(budget[field], int):
                    raise AgentIdentityValidationError(f"budget_policy.{field} must be an integer")
            for field in ("max_parallel_runs", "max_parallel_tasks"):
                if field in concurrency and not isinstance(concurrency[field], int):
                    raise AgentIdentityValidationError(f"concurrency_policy.{field} must be an integer")
            is_default_chat = self.is_default_chat_agent(agent)
            idempotent_p2 = {
                name: _effective_revision_p2_value(
                    is_default_chat_agent=is_default_chat,
                    existing=existing,
                    latest_existing=latest,
                    name=name,
                    requested=requested,
                )
                for name, requested in (
                    ("model_overrides", model_overrides),
                    ("tool_ids", tool_ids),
                    ("delegate_policy", delegate_policy),
                )
            }
            if is_default_chat and existing is None:
                # Aoi represents ordinary Chat. Keep its role routes on the
                # shared model_roles table and persist the concrete current
                # Chat catalog as the revision's explicit tool allowlist.
                from .agent_private_provisioning import _runtime_default_chat_projection

                current_chat_tool_ids = None
                if self.config is None and latest is not None:
                    from .agent_revision_v2 import revision_tool_ids

                    current_chat_tool_ids = revision_tool_ids(latest)
                if current_chat_tool_ids is None:
                    _chat_capabilities, current_chat_tool_ids = (
                        _runtime_default_chat_projection(self.config)
                    )
                model_values = {}
                selected_tools = list(current_chat_tool_ids)
                delegation = {"agents": "any_owned", "workers": []}
            else:
                revision_model_request = model_overrides
                revision_tool_request = tool_ids
                revision_delegate_request = delegate_policy
                if is_default_chat and existing is not None:
                    revision_model_request = (
                        idempotent_p2["model_overrides"]
                        if idempotent_p2["model_overrides"] is not None
                        else _P2_UNSET
                    )
                    revision_tool_request = (
                        idempotent_p2["tool_ids"]
                        if idempotent_p2["tool_ids"] is not None
                        else _P2_UNSET
                    )
                    revision_delegate_request = (
                        idempotent_p2["delegate_policy"]
                        if idempotent_p2["delegate_policy"] is not None
                        else _P2_UNSET
                    )
                derived_v2_defaults = None
                p2_omitted = any(
                    value is _P2_UNSET or value is None
                    for value in (
                        revision_model_request,
                        revision_tool_request,
                        revision_delegate_request,
                    )
                )
                previous_v2_missing = latest is None or any(
                    _optional_revision_value(latest, field) is None
                    for field in ("model_overrides", "tool_ids", "delegate_policy")
                )
                if p2_omitted and previous_v2_missing and not is_default_chat:
                    derived_v2_defaults = (
                        {},
                        list(ceiling),
                        {"agents": [], "workers": []},
                    )
                model_values, selected_tools, delegation = self._revision_v2_fields(
                    previous=latest,
                    model_overrides=revision_model_request,
                    tool_ids=revision_tool_request,
                    delegate_policy=revision_delegate_request,
                    capability_ceiling=ceiling,
                    derived_defaults=derived_v2_defaults,
                    allow_concrete_tool_ids=is_default_chat,
                )
            if is_default_chat and existing is None:
                model_values = _model_overrides(model_values)
                selected_tools = _tool_ids(selected_tools, allow_concrete=True)
                delegation = _delegate_policy(delegation)
            effective_p2 = {
                "model_overrides": model_values,
                "tool_ids": selected_tools,
                "delegate_policy": delegation,
            }
            content = {
                "agent_id": str(aid), "display_name": name, "mission": mission_text,
                "responsibility_summary": responsibility, "operational_instructions": instructions,
                # Team/Profile/Subagent pins are historical only. New
                # revisions resolve through model_roles and store no retired
                # topology columns.
                "agent_team_id": None, "execution_profile_id": None,
                "allowed_subagent_ids": None, "capability_ceiling": ceiling,
                "wake_policy": wake, "budget_policy": budget, "concurrency_policy": concurrency,
                "model_overrides": model_values, "tool_ids": selected_tools,
                "delegate_policy": delegation,
            }
            for p2_name, p2_value in effective_p2.items():
                if _revision_p2_column_available(p2_name):
                    content[p2_name] = p2_value
            digest = self._revision_content_hash(content)
            if content_hash not in (None, "") and str(content_hash).strip().lower() != digest:
                raise AgentIdentityValidationError("content_hash does not match revision content")
            if existing is not None:
                if not self._revision_content_matches(existing, content):
                    raise AgentIdentityConflictError("revision idempotency key was used with different content")
                return self._revision_projection(existing)
            if str(agent.state or "") == "retired":
                raise AgentIdentityConflictError("retired Agents cannot receive new revisions")
            latest_version = (
                await session.execute(
                    select(func.max(AgentRevision.version)).where(AgentRevision.agent_id == aid)
                )
            ).scalar_one()
            next_version = int(latest_version or 0) + 1
            chosen_version = int(version) if version is not None else next_version
            if chosen_version != next_version:
                raise AgentIdentityConflictError("revision version must be the next monotonically increasing version")
            row = AgentRevision(
                id=uuid4(), agent_id=aid, version=chosen_version, display_name=name,
                mission=mission_text, responsibility_summary=responsibility,
                operational_instructions=instructions, agent_team_id=None,
                execution_profile_id=None, allowed_subagent_ids=None,
                capability_ceiling_json=ceiling, wake_policy_json=wake,
                budget_policy_json=budget, concurrency_policy_json=concurrency,
                content_hash="0" * 64, idempotency_key=key, created_by=creator,
            )
            for p2_name, p2_value in effective_p2.items():
                if _revision_p2_column_available(p2_name):
                    _set_optional_revision_value(row, p2_name, p2_value)
            digest = self._revision_content_hash(row)
            row.content_hash = digest
            session.add(row)
            await session.flush()
            payload = self._revision_projection(row)
            await session.commit()
            return payload
        except IntegrityError:
            await session.rollback()
            winner = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid, AgentRevision.idempotency_key == key))).scalar_one_or_none()
            if winner is not None and self._revision_content_matches(winner, content):
                return self._revision_projection(winner)
            if winner is not None:
                raise AgentIdentityConflictError("revision idempotency key was used with different content")
            raise AgentIdentityConflictError("Agent revision creation conflicted")
        finally:
            await session.close()

    create_agent_revision = create_revision
    append_agent_revision = create_revision

    async def save_personal_adapter_agent(
        self,
        *,
        owner_user_id: Any,
        display_name: Any,
        profile_id: Any,
        idempotency_key: Any,
        agent_id: Any | None = None,
    ) -> dict[str, Any]:
        """Create or append a revision to the caller's bounded personal Agent.

        This is deliberately narrower than generic Agent CRUD: the owner is
        verified from the database, the team/subagent/profile are code-owned,
        and the ceiling is selected from the existing personal profile map.
        No prompt, provider, route, grant, or scheduling input is accepted.
        """
        owner = _uuid(owner_user_id, "owner_user_id")
        name = _text(display_name, "display_name", max_length=160)
        profile = _text(profile_id, "profile_id", max_length=64)
        capabilities = _PERSONAL_ADAPTER_PROFILE_CAPABILITIES.get(profile)
        if capabilities is None:
            raise AgentIdentityValidationError("unsupported personal adapter profile")
        request_key = _idempotency(idempotency_key)
        requested_agent = _uuid(agent_id, "agent_id", required=False)
        ceiling = _capability_list(list(capabilities))
        # Personal adapters are a bounded non-Team Specialist lane. Their
        # current policy is represented entirely by v2 fields.
        model_overrides: dict[str, Any] = {}
        tool_ids = list(ceiling)
        delegate_policy = {"agents": [], "workers": []}

        agent_key = f"personal-adapter:{owner}:agent:{request_key}"
        revision_key = f"personal-adapter:{owner}:revision:{request_key}"
        mission = "本人が選択した個人用adapter操作を手動で補助します。"
        responsibility = "個人専用。明示選択されたPersonal Agent adapterの読取または未受理候補だけを扱います。"
        instructions = "ローカル限定の個人操作です。台帳の確定は行わず、候補は必ず本人の確認と受理を待ちます。"
        wake = {"mode": "manual"}
        budget: dict[str, Any] = {}
        concurrency: dict[str, Any] = {}
        session = await self._session()
        try:
            await self._assert_active_human_owner(session, owner)
            if requested_agent is None:
                agent = (await session.execute(
                    select(Agent).where(Agent.idempotency_key == agent_key).with_for_update()
                )).scalar_one_or_none()
                create_hash = sha256_json({"display_name": name, "slug": None, "character_id": None})
                if agent is None:
                    agent = Agent(
                        id=uuid4(), display_name=name, slug=None, state="draft", character_id=None,
                        create_hash=create_hash, idempotency_key=agent_key, created_by=owner,
                    )
                    session.add(agent)
                    await session.flush()
                elif agent.created_by != owner or agent.create_hash != create_hash:
                    raise AgentIdentityConflictError("personal Agent idempotency key conflicts")
            else:
                agent = (await session.execute(
                    select(Agent).where(Agent.id == requested_agent).with_for_update()
                )).scalar_one_or_none()
                if agent is None or agent.created_by != owner:
                    raise AgentIdentityNotFoundError("personal Agent not found")
                if str(agent.state or "") != "active":
                    raise AgentIdentityConflictError("personal Agent must be active")
                latest_existing = (await session.execute(
                    select(AgentRevision).where(AgentRevision.agent_id == agent.id)
                    .order_by(AgentRevision.version.desc()).limit(1)
                )).scalar_one_or_none()
                if latest_existing is None or set(latest_existing.capability_ceiling_json or []) != set(ceiling):
                    raise AgentIdentityAuthorizationError("Agent is not an owned personal adapter")

            content = {
                "agent_id": str(agent.id), "display_name": name, "mission": mission,
                "responsibility_summary": responsibility, "operational_instructions": instructions,
                "agent_team_id": None, "execution_profile_id": None,
                "allowed_subagent_ids": None, "capability_ceiling": ceiling,
                "wake_policy": wake, "budget_policy": budget, "concurrency_policy": concurrency,
                "model_overrides": model_overrides, "tool_ids": tool_ids,
                "delegate_policy": delegate_policy,
            }
            content_hash = self._revision_content_hash(content)
            existing = (await session.execute(
                select(AgentRevision).where(
                    AgentRevision.agent_id == agent.id,
                    AgentRevision.idempotency_key == revision_key,
                ).with_for_update()
            )).scalar_one_or_none()
            if existing is not None:
                if (
                    not self._revision_content_matches(existing, content)
                    or existing.created_by != owner
                ):
                    raise AgentIdentityConflictError("personal Agent revision idempotency key conflicts")
                revision = existing
            else:
                latest = (await session.execute(
                    select(func.max(AgentRevision.version)).where(AgentRevision.agent_id == agent.id)
                )).scalar_one()
                revision = AgentRevision(
                    id=uuid4(), agent_id=agent.id, version=int(latest or 0) + 1,
                    display_name=name, mission=mission, responsibility_summary=responsibility,
                    operational_instructions=instructions, agent_team_id=None,
                    execution_profile_id=None, allowed_subagent_ids=None,
                    capability_ceiling_json=ceiling, wake_policy_json=wake,
                    budget_policy_json=budget, concurrency_policy_json=concurrency,
                    model_overrides=model_overrides, tool_ids=tool_ids,
                    delegate_policy=delegate_policy,
                    content_hash=content_hash, idempotency_key=revision_key, created_by=owner,
                )
                session.add(revision)
                await session.flush()
            self._assert_revision_content_hash(revision)
            if requested_agent is None:
                agent.state = "active"
            agent.display_name = name
            agent.updated_at = datetime.utcnow()
            await session.commit()
            return {
                "agent_id": str(agent.id), "display_name": name, "state": str(agent.state),
                "profile_id": profile, "agent_revision_id": str(revision.id),
                "revision_version": int(revision.version),
                "model_overrides": revision.model_overrides,
                "tool_ids": revision.tool_ids,
                "delegate_policy": revision.delegate_policy,
                "effective_model_routes": self._effective_model_routes(revision.to_safe_dict()),
            }
        except IntegrityError:
            await session.rollback()
            raise AgentIdentityConflictError("personal Agent save conflicted") from None
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def list_personal_adapter_agents(self, *, owner_user_id: Any) -> list[dict[str, Any]]:
        owner = _uuid(owner_user_id, "owner_user_id")
        session = await self._session()
        try:
            await self._assert_active_human_owner(session, owner)
            agents = (await session.execute(
                select(Agent).where(Agent.created_by == owner, Agent.state == "active")
                .order_by(Agent.updated_at.desc()).limit(100)
            )).scalars().all()
            result = []
            for agent in agents:
                revision = (await session.execute(
                    select(AgentRevision).where(AgentRevision.agent_id == agent.id)
                    .order_by(AgentRevision.version.desc()).limit(1)
                )).scalar_one_or_none()
                if revision is None or revision.model_overrides is None or revision.tool_ids is None:
                    continue
                profile = next((key for key, value in _PERSONAL_ADAPTER_PROFILE_CAPABILITIES.items()
                                if set(value) == set(revision.capability_ceiling_json or [])), None)
                if profile is None:
                    continue
                result.append({
                    "agent_id": str(agent.id), "display_name": revision.display_name,
                    "state": str(agent.state), "profile_id": profile,
                    "agent_revision_id": str(revision.id), "revision_version": int(revision.version),
                })
            return result
        finally:
            await session.close()

    async def list_revisions(self, agent_id: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        aid = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            rows = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid).order_by(AgentRevision.version.desc()).limit(min(max(int(limit or 100), 1), 200)))).scalars().all()
            return [self._revision_projection(row) for row in rows]
        finally:
            await session.close()

    list_agent_revisions = list_revisions

    async def get_revision(self, revision_id: Any) -> dict[str, Any] | None:
        rid = _uuid(revision_id, "agent_revision_id")
        session = await self._session()
        try:
            row = await session.get(AgentRevision, rid)
            return self._revision_projection(row) if row else None
        finally:
            await session.close()

    get_agent_revision = get_revision

    async def upsert_organization_profile(self, *, agent_id: Any, payload: Mapping[str, Any], actor_user_id: Any) -> dict[str, Any]:
        if not Features.is_enabled("virtual_company"):
            raise AgentIdentityFeatureDisabledError("virtual company features are disabled")
        aid = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
            if await session.get(Agent, aid) is None:
                raise AgentIdentityNotFoundError("Agent not found")
            from .agent_organization_service import lock_reporting_graph, validate_manager_graph

            await lock_reporting_graph(session)
            row = await session.get(AgentOrganizationProfile, aid)
            primary_space = _uuid(
                payload["primary_space_id"]
                if "primary_space_id" in payload
                else (row.primary_space_id if row else None),
                "primary_space_id",
                required=False,
            )
            if primary_space is not None and await session.get(Space, primary_space) is None:
                raise AgentIdentityNotFoundError("Space not found")
            manager_user = _uuid(
                payload["manager_user_id"] if "manager_user_id" in payload else (row.manager_user_id if row else None),
                "manager_user_id",
                required=False,
            )
            manager_agent = _uuid(
                payload["manager_agent_id"] if "manager_agent_id" in payload else (row.manager_agent_id if row else None),
                "manager_agent_id",
                required=False,
            )
            if manager_user is not None and manager_agent is not None:
                raise AgentIdentityValidationError(
                    "manager_user_id and manager_agent_id are mutually exclusive"
                )
            if manager_user is not None:
                manager_row = await session.get(User, manager_user)
                if manager_row is None or not bool(manager_row.is_active):
                    raise AgentIdentityNotFoundError("manager user not found")
            if manager_agent is not None:
                if manager_agent == aid:
                    raise AgentIdentityValidationError("an Agent cannot manage itself")
                manager_row = await session.get(Agent, manager_agent)
                if manager_row is None or str(manager_row.state or "") != "active":
                    raise AgentIdentityNotFoundError("manager Agent not found")
            await validate_manager_graph(session, aid, manager_agent)
            autonomy = _text(
                payload.get("autonomy_level", row.autonomy_level if row else "supervised"),
                "autonomy_level",
                max_length=16,
            )
            if autonomy not in {"disabled", "supervised", "bounded", "autonomous"}:
                raise AgentIdentityValidationError("invalid autonomy_level")
            employment = _text(
                payload.get("employment_state", row.employment_state if row else "active"),
                "employment_state",
                max_length=16,
            )
            if employment not in {"active", "on_leave", "suspended", "terminated", "contractor"}:
                raise AgentIdentityValidationError("invalid employment_state")
            ceiling = _safe_policy(
                payload.get(
                    "company_permission_ceiling",
                    row.company_permission_ceiling_json if row else None,
                ),
                label="company_permission_ceiling",
                allowed_keys={"permissions", "capabilities", "projects", "spaces", "personas"},
            )
            if "permissions" in ceiling:
                raw_permissions = ceiling["permissions"]
                if isinstance(raw_permissions, Mapping):
                    if any(
                        key not in PROJECT_PERMISSION_KEYS or not isinstance(value, bool)
                        for key, value in raw_permissions.items()
                    ):
                        raise AgentIdentityValidationError("invalid company permission ceiling")
                    ceiling["permissions"] = dict(raw_permissions)
                else:
                    ceiling["permissions"] = _string_list(
                        raw_permissions,
                        label="company_permission_ceiling.permissions",
                        max_length=64,
                        allowed=set(PROJECT_PERMISSION_KEYS),
                    )
            if "capabilities" in ceiling:
                ceiling["capabilities"] = _capability_list(
                    ceiling["capabilities"], label="company_permission_ceiling.capabilities"
                )
            if row is None:
                row = AgentOrganizationProfile(agent_id=aid)
                session.add(row)
            row.job_title = _text(payload.get("job_title", row.job_title), "job_title", max_length=160, required=False)
            row.responsibility_summary = _text(payload.get("responsibility_summary", row.responsibility_summary), "responsibility_summary", max_length=10_000, required=False)
            row.primary_space_id = primary_space
            row.manager_user_id = manager_user
            row.manager_agent_id = manager_agent
            row.autonomy_level = autonomy
            row.company_permission_ceiling_json = ceiling
            row.employment_state = employment
            row.updated_at = datetime.utcnow()
            result = row.to_safe_dict()
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def get_organization_profile(self, agent_id: Any) -> dict[str, Any] | None:
        aid = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            row = await session.get(AgentOrganizationProfile, aid)
            return row.to_safe_dict() if row else None
        finally:
            await session.close()

    async def _assert_assignment_target(self, session, model: Any, target_id: UUID, label: str) -> None:
        row = await session.get(model, target_id)
        if row is None:
            raise AgentIdentityNotFoundError(f"{label} not found")
        if getattr(row, "deleted_at", None) is not None:
            raise AgentIdentityNotFoundError(f"{label} not found")
        if label == "Persona" and str(getattr(row, "state", "")) in {"archived", "retired"}:
            raise AgentIdentityNotFoundError(f"{label} not found")

    async def create_space_assignment(self, *, agent_id: Any, space_id: Any, assignment_kind: Any = "supporting", role_label: Any = None, policy_ceiling: Any = None, active_from: Any = None, active_until: Any = None, actor_user_id: Any) -> dict[str, Any]:
        kind = _text(assignment_kind, "assignment_kind", max_length=16)
        if kind not in {"primary", "secondary", "supporting"}:
            raise AgentIdentityValidationError("invalid assignment_kind")
        label = _text(role_label, "role_label", max_length=120, required=False) or None
        policy = _safe_policy(
            policy_ceiling,
            label="policy_ceiling",
            allowed_keys={"allowed_capabilities", "permissions", "max_concurrent_runs"},
        )
        if "allowed_capabilities" in policy:
            policy["allowed_capabilities"] = _capability_list(policy["allowed_capabilities"], label="policy_ceiling.allowed_capabilities")
        if "permissions" in policy:
            policy["permissions"] = _string_list(
                policy["permissions"],
                label="policy_ceiling.permissions",
                max_length=64,
                allowed=set(PROJECT_PERMISSION_KEYS),
            )
        return await self._create_assignment(
            AgentSpaceAssignment,
            agent_id=agent_id, target_id=space_id, target_model=Space, target_label="Space",
            actor_user_id=actor_user_id, values={"assignment_kind": kind, "role_label": label, "policy_ceiling_json": policy, "active_from": active_from, "active_until": active_until},
        )

    async def create_project_grant(self, *, agent_id: Any, project_id: Any, role: Any = "viewer", permissions: Any = None, active_from: Any = None, active_until: Any = None, actor_user_id: Any) -> dict[str, Any]:
        normalized_role = normalize_project_member_role(role)
        normalized_permissions = get_default_project_permissions(normalized_role) if permissions is None else normalize_project_member_permissions(permissions)
        if permissions is not None and (
            not isinstance(permissions, Mapping)
            or any(key not in PROJECT_PERMISSION_KEYS or not isinstance(value, bool) for key, value in permissions.items())
        ):
            raise AgentIdentityValidationError("invalid project permissions")
        return await self._create_assignment(
            AgentProjectGrant,
            agent_id=agent_id, target_id=project_id, target_model=Project, target_label="Project",
            actor_user_id=actor_user_id, values={"role": normalized_role, "permissions": normalized_permissions, "active_from": active_from, "active_until": active_until},
        )

    async def create_task_assignment(self, *, agent_id: Any, task_id: Any, assignment_role: Any = "executor", active_from: Any = None, active_until: Any = None, actor_user_id: Any) -> dict[str, Any]:
        role = _text(assignment_role, "assignment_role", max_length=16)
        if role not in {"owner", "executor", "reviewer", "observer"}:
            raise AgentIdentityValidationError("invalid assignment_role")
        return await self._create_assignment(
            AgentTaskAssignment,
            agent_id=agent_id, target_id=task_id, target_model=Task, target_label="Task",
            actor_user_id=actor_user_id, values={"assignment_role": role, "active_from": active_from, "active_until": active_until},
        )

    async def create_persona_operator_assignment(self, *, agent_id: Any, persona_id: Any, role: Any = "operator", is_primary: bool = False, capability_ceiling: Any = None, active_from: Any = None, active_until: Any = None, actor_user_id: Any) -> dict[str, Any]:
        role_text = _text(role, "role", max_length=16)
        if role_text not in {"operator", "strategist", "researcher", "creator", "analyst", "publisher"}:
            raise AgentIdentityValidationError("invalid Persona operator role")
        return await self._create_assignment(
            PersonaOperatorAssignment,
            agent_id=agent_id, target_id=persona_id, target_model=Persona, target_label="Persona",
            actor_user_id=actor_user_id, values={"role": role_text, "is_primary": bool(is_primary), "capability_ceiling": _capability_list(capability_ceiling), "active_from": active_from, "active_until": active_until},
        )

    async def update_persona_operator_ceiling(self, *, agent_id: Any, assignment_id: Any, capability_ceiling: Any, expected_capability_ceiling: Any, actor_user_id: Any) -> dict[str, Any]:
        if not Features.is_enabled("virtual_company"):
            raise AgentIdentityFeatureDisabledError("virtual company features are disabled")
        aid = _uuid(agent_id, "agent_id")
        rid = _uuid(assignment_id, "assignment_id")
        ceiling = _capability_list(capability_ceiling)
        expected = _capability_list(expected_capability_ceiling, label="expected_capability_ceiling")
        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
            row = await session.get(PersonaOperatorAssignment, rid, with_for_update=True)
            if row is None or row.agent_id != aid:
                raise AgentIdentityNotFoundError("assignment not found")
            if row.state != "active":
                raise AgentIdentityConflictError("inactive assignments cannot be edited")
            await self._assert_assignment_target(session, Persona, row.persona_id, "Persona")
            persona = await session.get(Persona, row.persona_id)
            # Preserve the owner's operating authorization for a personal Persona.
            # Another administrator must not widen a grant issued by its owner.
            if persona.project_id is None and persona.owner_user_id is not None and persona.owner_user_id != _uuid(actor_user_id, "actor_user_id"):
                raise AgentIdentityAuthorizationError("personal Persona ceiling requires its owner")
            if row.capability_ceiling_json != expected:
                raise AgentIdentityConflictError("Persona capability ceiling changed")
            row.capability_ceiling_json = ceiling
            row.updated_at = datetime.utcnow()
            result = row.to_safe_dict()
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _create_assignment(self, model: Any, *, agent_id: Any, target_id: Any, target_model: Any, target_label: str, actor_user_id: Any, values: dict[str, Any]) -> dict[str, Any]:
        if not Features.is_enabled("virtual_company"):
            raise AgentIdentityFeatureDisabledError("virtual company features are disabled")
        aid = _uuid(agent_id, "agent_id")
        tid = _uuid(target_id, f"{target_label.lower()}_id")
        active_from = _when(values.pop("active_from", None), "active_from")
        active_until = _when(values.pop("active_until", None), "active_until")
        _check_dates(active_from, active_until)
        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
            agent = await session.get(Agent, aid)
            if agent is None:
                raise AgentIdentityNotFoundError("Agent not found")
            await self._assert_assignment_target(session, target_model, tid, target_label)
            target_field = {
                AgentSpaceAssignment: "space_id",
                AgentProjectGrant: "project_id",
                AgentTaskAssignment: "task_id",
                PersonaOperatorAssignment: "persona_id",
            }.get(model)
            if not target_field:
                raise AgentIdentityValidationError("unsupported assignment model")
            actor_field = "granted_by" if model is AgentProjectGrant else "assigned_by"
            kwargs = {
                "id": uuid4(),
                "agent_id": aid,
                target_field: tid,
                "state": "active",
                actor_field: _uuid(actor_user_id, "actor_user_id"),
                "active_from": active_from,
                "active_until": active_until,
                **values,
            }
            row = model(**kwargs)
            session.add(row)
            await session.flush()
            payload = row.to_safe_dict()
            await session.commit()
            return payload
        except IntegrityError:
            await session.rollback()
            raise AgentIdentityConflictError("an equivalent active assignment already exists")
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def list_assignments(self, model: Any, *, agent_id: Any, include_inactive: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        aid = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            stmt = select(model).where(model.agent_id == aid).order_by(model.created_at.desc()).limit(min(max(int(limit or 200), 1), 500))
            if not include_inactive:
                stmt = stmt.where(model.state == "active")
            return [row.to_safe_dict() for row in (await session.execute(stmt)).scalars().all()]
        finally:
            await session.close()

    async def transition_assignment(self, model: Any, assignment_id: Any, state: Any, *, actor_user_id: Any, agent_id: Any | None = None) -> dict[str, Any]:
        """Revoke/expire an assignment without deleting audit history."""

        target_state = _text(state, "state", max_length=16)
        if target_state not in {"active", "revoked", "expired"}:
            raise AgentIdentityValidationError("invalid assignment state")
        rid = _uuid(assignment_id, "assignment_id")
        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
            row = await session.get(model, rid)
            if row is None:
                raise AgentIdentityNotFoundError("assignment not found")
            if agent_id is not None and row.agent_id != _uuid(agent_id, "agent_id"):
                raise AgentIdentityNotFoundError("assignment not found")
            # A revoked/expired row is immutable history; reactivation is
            # intentionally denied so a new human grant is required.
            if str(row.state or "") != "active" and target_state == "active":
                raise AgentIdentityConflictError("inactive assignments cannot be reactivated")
            row.state = target_state
            row.updated_at = datetime.utcnow()
            result = row.to_safe_dict()
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def revoke_assignment(self, model: Any, assignment_id: Any, *, actor_user_id: Any, agent_id: Any | None = None) -> dict[str, Any]:
        return await self.transition_assignment(model, assignment_id, "revoked", actor_user_id=actor_user_id, agent_id=agent_id)


# Compatibility names make the service discoverable to future workstreams
# without creating Company-specific identity classes.
AgentService = AgentIdentityService
OrganizationSettingsService = OrganizationService
OrganizationBootstrapService = OrganizationService


__all__ = [
    "AOI_DEFAULT_AGENT_KIND",
    "AOI_DEFAULT_AGENT_KEY_VERSION",
    "aoi_default_agent_idempotency_key",
    "AgentIdentityError",
    "AgentIdentityValidationError",
    "AgentIdentityNotFoundError",
    "AgentIdentityConflictError",
    "AgentIdentityAuthorizationError",
    "AgentIdentityFeatureDisabledError",
    "canonical_json",
    "sha256_json",
    "OrganizationService",
    "OrganizationSettingsService",
    "OrganizationBootstrapService",
    "AgentIdentityService",
    "AgentService",
]


def __getattr__(name: str):
    """Lazy compatibility export for the separate authority resolver."""

    if name in {"AgentAuthorityResolver", "AuthorityDecision"}:
        from .agent_authority import AgentAuthorityResolver, AuthorityDecision

        value = {
            "AgentAuthorityResolver": AgentAuthorityResolver,
            "AuthorityDecision": AuthorityDecision,
        }[name]
        globals()[name] = value
        return value
    raise AttributeError(name)
