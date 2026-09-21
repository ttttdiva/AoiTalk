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
from datetime import datetime
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
from .agent_team_v3 import (
    AGENT_TEAM_CAPABILITY_CATALOG,
    AGENT_TEAM_DEFAULT_TEAMS,
    AGENT_TEAM_SUBAGENT_CATALOG,
    AGENT_TEAM_DEFAULT_LLM_PROFILES,
    agent_team_v3_subagents,
    agent_team_v3_teams,
)
from .execution_profile_service import list_team_execution_profiles
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
        if capability not in AGENT_TEAM_CAPABILITY_CATALOG:
            raise AgentIdentityValidationError(f"unknown capability: {capability}")
        if capability not in result:
            result.append(capability)
    return result


def _string_list(value: Any, *, label: str, max_length: int = 100, allowed: set[str] | None = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise AgentIdentityValidationError(f"{label} must be a list")
    if len(value) > _MAX_LIST_ITEMS:
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
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
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

    def _known_teams(self) -> dict[str, dict[str, Any]]:
        known = {str(key): value for key, value in AGENT_TEAM_DEFAULT_TEAMS.items()}
        try:
            for item in agent_team_v3_teams(self.config):
                if isinstance(item, Mapping) and str(item.get("team_id") or "").strip():
                    known[str(item["team_id"])] = dict(item)
        except Exception:
            logger.debug("agent team config unavailable; using canonical defaults", exc_info=True)
        return known

    async def employee_catalog(self, *, actor_user_id: Any) -> dict[str, Any]:
        """Safe editor choices from the same catalogs used at revision save."""
        session = await self._session()
        try:
            await self._assert_admin(session, actor_user_id)
        finally:
            await session.close()
        subagents = dict(AGENT_TEAM_SUBAGENT_CATALOG)
        for item in agent_team_v3_subagents(self.config):
            subagents[str(item["subagent_id"])] = item
        teams = []
        for team_id, team in self._known_teams().items():
            if team.get("enabled") is False:
                continue
            members = [
                {"subagent_id": sid, "name": subagents[sid].get("name", sid),
                 "capability_ids": list(subagents[sid].get("capability_ids") or [])}
                for sid in team.get("subagent_ids", [])
                if sid in subagents and subagents[sid].get("enabled") is not False
            ]
            profiles = [{"profile_id": "manual", "name": "標準モデル"},
                        {"profile_id": "free-team", "name": "Free Team"}]
            profiles.extend(
                {"profile_id": item["profile_id"], "name": item.get("name", item["profile_id"])}
                for item in list_team_execution_profiles(self.config, team_id)
                if item.get("enabled") is not False and item["profile_id"] not in {"manual", "free-team"}
            )
            teams.append({"team_id": team_id, "name": team.get("name", team_id),
                          "subagents": members, "execution_profiles": profiles})
        return {"teams": teams, "capabilities": list(AGENT_TEAM_CAPABILITY_CATALOG.values()),
                "can_manage": True, "runtime_profile": Features.profile_name()}

    def _validate_team_refs(
        self,
        team_id: Any,
        execution_profile_id: Any,
        allowed_subagent_ids: Any,
        capabilities: Any,
    ) -> tuple[str, str, list[str], list[str]]:
        team = _text(team_id, "agent_team_id", max_length=100)
        known_teams = self._known_teams()
        if team not in known_teams:
            raise AgentIdentityValidationError(f"unknown agent team: {team}")
        profile = _text(execution_profile_id, "execution_profile_id", max_length=100)
        if not _ID_RE.fullmatch(profile):
            raise AgentIdentityValidationError("invalid execution_profile_id")
        profiles = list_team_execution_profiles(self.config, team)
        profile_ids = {str(item.get("profile_id") or "") for item in profiles if isinstance(item, Mapping)}
        # System profiles are valid in every team; a configured team with a
        # non-system profile must explicitly contain that profile.
        canonical_profile_ids = {"manual", "free-team", *AGENT_TEAM_DEFAULT_LLM_PROFILES}
        if profile not in canonical_profile_ids and profile_ids and profile not in profile_ids:
            raise AgentIdentityValidationError(f"unknown execution profile for team: {profile}")
        if profile not in canonical_profile_ids and not profile_ids:
            raise AgentIdentityValidationError(f"unknown execution profile: {profile}")
        configured_subagents = [
            item for item in agent_team_v3_subagents(self.config)
            if isinstance(item, Mapping) and item.get("subagent_id") and item.get("enabled") is not False
        ]
        known_subagents = {
            key for key, value in AGENT_TEAM_SUBAGENT_CATALOG.items()
            if value.get("enabled", True) is not False
        }
        known_subagents.update(str(item.get("subagent_id") or "") for item in configured_subagents)
        subagents = _string_list(allowed_subagent_ids, label="allowed_subagent_ids", allowed=known_subagents)
        team_members = {
            str(item).strip()
            for item in (known_teams[team].get("subagent_ids") or [])
            if str(item).strip()
        }
        if team_members and any(item not in team_members for item in subagents):
            raise AgentIdentityValidationError("allowed_subagent_ids must belong to the referenced Agent Team")
        ceiling = _capability_list(capabilities)
        return team, profile, subagents, ceiling

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
                return existing.to_safe_dict()
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
            payload = row.to_safe_dict()
            await session.commit()
            return payload
        except IntegrityError:
            await session.rollback()
            winner = (await session.execute(select(Agent).where(Agent.idempotency_key == key))).scalar_one_or_none()
            if winner is not None and winner.create_hash == create_hash:
                return winner.to_safe_dict()
            raise AgentIdentityConflictError("Agent creation conflicted")
        finally:
            await session.close()

    async def list_agents(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit or 100), 1), 200)
        session = await self._session()
        try:
            stmt = select(Agent).order_by(Agent.created_at.desc()).limit(safe_limit)
            if state:
                if state not in {"draft", "active", "paused", "retired"}:
                    raise AgentIdentityValidationError("invalid agent state")
                stmt = stmt.where(Agent.state == state)
            return [item.to_safe_dict() for item in (await session.execute(stmt)).scalars().all()]
        finally:
            await session.close()

    async def get_agent(self, agent_id: Any) -> dict[str, Any] | None:
        parsed = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            row = await session.get(Agent, parsed)
            return row.to_safe_dict() if row else None
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
            await self._assert_admin(session, actor_user_id)
            row = await session.get(Agent, _uuid(agent_id, "agent_id"))
            if row is None:
                raise AgentIdentityNotFoundError("Agent not found")
            if target not in self._STATE_TRANSITIONS.get(str(row.state), set()):
                raise AgentIdentityConflictError(f"invalid Agent state transition: {row.state} -> {target}")
            if expected_state is not None and expected_state != row.state:
                raise AgentIdentityConflictError("Agent state changed; reload before saving")
            if target == "active":
                revision = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == row.id).order_by(AgentRevision.version.desc()).limit(1))).scalar_one_or_none()
                if revision is None:
                    raise AgentIdentityConflictError("Agent revision is required before activation")
                self._validate_team_refs(revision.agent_team_id, revision.execution_profile_id,
                                         revision.allowed_subagent_ids, revision.capability_ceiling_json)
            changed = await session.execute(update(Agent).where(Agent.id == row.id, Agent.state == row.state)
                .values(state=target, updated_at=datetime.utcnow()))
            if changed.rowcount != 1:
                raise AgentIdentityConflictError("Agent state changed; reload before saving")
            await session.commit()
            await session.refresh(row)
            return row.to_safe_dict()
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
        display_name: Any,
        mission: Any = "",
        responsibility_summary: Any = "",
        operational_instructions: Any = "",
        agent_team_id: Any,
        execution_profile_id: Any,
        allowed_subagent_ids: Any = None,
        capability_ceiling: Any = None,
        wake_policy: Any = None,
        budget_policy: Any = None,
        concurrency_policy: Any = None,
        idempotency_key: Any,
        actor_user_id: Any | None = None,
        created_by_user_id: Any | None = None,
        created_by: Any | None = None,
        version: int | None = None,
        content_hash: Any | None = None,
    ) -> dict[str, Any]:
        aid = _uuid(agent_id, "agent_id")
        name = _text(display_name, "display_name", max_length=160)
        mission_text = _text(mission, "mission", max_length=10_000, required=False)
        responsibility = _text(responsibility_summary, "responsibility_summary", max_length=10_000, required=False)
        instructions = _text(operational_instructions, "operational_instructions", max_length=30_000, required=False)
        key = _idempotency(idempotency_key)
        team, profile, subagents, ceiling = self._validate_team_refs(
            agent_team_id, execution_profile_id, allowed_subagent_ids, capability_ceiling
        )
        wake = _safe_policy(wake_policy, label="wake_policy", allowed_keys=self._WAKE_KEYS)
        budget = _safe_policy(budget_policy, label="budget_policy", allowed_keys=self._BUDGET_KEYS)
        concurrency = _safe_policy(concurrency_policy, label="concurrency_policy", allowed_keys=self._CONCURRENCY_KEYS)
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
        creator = _uuid(
            actor_user_id if actor_user_id is not None else (
                created_by_user_id if created_by_user_id is not None else created_by
            ),
            "actor_user_id",
            required=False,
        )
        session = await self._session()
        try:
            await self._assert_admin(session, creator)
            agent = await session.get(Agent, aid)
            if agent is None:
                raise AgentIdentityNotFoundError("Agent not found")
            existing = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid, AgentRevision.idempotency_key == key))).scalar_one_or_none()
            content = {
                "agent_id": str(aid), "display_name": name, "mission": mission_text,
                "responsibility_summary": responsibility, "operational_instructions": instructions,
                "agent_team_id": team, "execution_profile_id": profile,
                "allowed_subagent_ids": subagents, "capability_ceiling": ceiling,
                "wake_policy": wake, "budget_policy": budget, "concurrency_policy": concurrency,
            }
            digest = sha256_json(content)
            if content_hash not in (None, "") and str(content_hash).strip().lower() != digest:
                raise AgentIdentityValidationError("content_hash does not match revision content")
            if existing is not None:
                if existing.content_hash != digest:
                    raise AgentIdentityConflictError("revision idempotency key was used with different content")
                return existing.to_safe_dict()
            if str(agent.state or "") == "retired":
                raise AgentIdentityConflictError("retired Agents cannot receive new revisions")
            latest = (await session.execute(select(func.max(AgentRevision.version)).where(AgentRevision.agent_id == aid))).scalar_one()
            next_version = int(latest or 0) + 1
            chosen_version = int(version) if version is not None else next_version
            if chosen_version != next_version:
                raise AgentIdentityConflictError("revision version must be the next monotonically increasing version")
            row = AgentRevision(
                id=uuid4(), agent_id=aid, version=chosen_version, display_name=name,
                mission=mission_text, responsibility_summary=responsibility,
                operational_instructions=instructions, agent_team_id=team,
                execution_profile_id=profile, allowed_subagent_ids=subagents,
                capability_ceiling_json=ceiling, wake_policy_json=wake,
                budget_policy_json=budget, concurrency_policy_json=concurrency,
                content_hash=digest, idempotency_key=key, created_by=creator,
            )
            session.add(row)
            await session.flush()
            payload = row.to_safe_dict()
            await session.commit()
            return payload
        except IntegrityError:
            await session.rollback()
            winner = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid, AgentRevision.idempotency_key == key))).scalar_one_or_none()
            if winner is not None and winner.content_hash == digest:
                return winner.to_safe_dict()
            if winner is not None:
                raise AgentIdentityConflictError("revision idempotency key was used with different content")
            raise AgentIdentityConflictError("Agent revision creation conflicted")
        finally:
            await session.close()

    create_agent_revision = create_revision
    append_agent_revision = create_revision

    async def list_revisions(self, agent_id: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        aid = _uuid(agent_id, "agent_id")
        session = await self._session()
        try:
            rows = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid).order_by(AgentRevision.version.desc()).limit(min(max(int(limit or 100), 1), 200)))).scalars().all()
            return [row.to_safe_dict() for row in rows]
        finally:
            await session.close()

    list_agent_revisions = list_revisions

    async def get_revision(self, revision_id: Any) -> dict[str, Any] | None:
        rid = _uuid(revision_id, "agent_revision_id")
        session = await self._session()
        try:
            row = await session.get(AgentRevision, rid)
            return row.to_safe_dict() if row else None
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
