"""Proposal-first Skill mutation and actual-use evidence service."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    AgentRun,
    ConversationMessage,
    Project,
    ProjectMember,
    SkillProposal,
    SkillProposalHistory,
    SkillUsageReceipt,
    User,
)
from ..security.field_crypto import redact_secret_value
from ..security.skill_content_privacy import (
    SkillContentPrivacyError,
    assert_no_secret_like_skill_content,
    redact_secret_like_skill_content,
)
from .scoped_memory_service import classify_sensitivity
from .skill_target_io import (
    SkillTargetTransitionPending,
    async_skill_target_lock,
    atomic_remove,
    atomic_replace_text,
    clear_skill_transition_journal,
    read_skill_transition_journal,
    read_stable_skill_text,
    read_target_hash,
    write_skill_transition_journal,
)

logger = logging.getLogger(__name__)

_SKILL_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,127}$")
_MAX_PROMPT_CHARS = 131_072
_MAX_PROVENANCE_BYTES = 32_768
_MAX_STRING_CHARS = 2_048
_MAX_LIST_ITEMS = 32
_MAX_DICT_ITEMS = 64

# ``/masking`` is a trusted built-in operation rather than a file-backed
# Skill.  Keep its receipt identity deliberately synthetic and constant: a
# masking receipt must never carry the source path, filename, content, or the
# detector's entity/alias map.  We keep the receipt in the existing global
# scope so the normal global/project scope contract remains unchanged and no
# proposal can accidentally target it.
MASKING_INVOCATION_PATH = "masking"
MASKING_RECEIPT_SKILL_NAME = "masking"
MASKING_RECEIPT_SKILL_PATH = "builtin:masking"
MASKING_RECEIPT_SKILL_VERSION = "builtin:masking:v1"
MASKING_RECEIPT_SKILL_HASH = hashlib.sha256(
    MASKING_RECEIPT_SKILL_VERSION.encode("utf-8")
).hexdigest()


def _is_builtin_masking_receipt(receipt: Any) -> bool:
    """Return whether a receipt is the one-way built-in masking record."""

    return (
        str(getattr(receipt, "invocation_path", "") or "").strip().lower()
        == MASKING_INVOCATION_PATH
        or (
            str(getattr(receipt, "skill_name", "") or "").strip().lower()
            == MASKING_RECEIPT_SKILL_NAME
            and str(getattr(receipt, "skill_path", "") or "").strip()
            == MASKING_RECEIPT_SKILL_PATH
        )
    )


class SkillLearningError(Exception):
    pass


class SkillLearningValidationError(SkillLearningError):
    pass


class SkillLearningForbidden(SkillLearningError):
    pass


class SkillLearningNotFound(SkillLearningError):
    pass


class SkillLearningConflict(SkillLearningError):
    pass


class SkillLearningStale(SkillLearningConflict):
    def __init__(self, message: str, proposal: dict[str, Any]):
        super().__init__(message)
        self.proposal = proposal


@dataclass(frozen=True)
class SkillTargetSnapshot:
    scope: str
    name: str
    project_id: Optional[str]
    path: Path
    canonical_path: str
    exists: bool
    text: Optional[str]
    payload: Optional[dict[str, Any]]
    sha256: Optional[str]
    version: Optional[str]


def _uuid(value: Any) -> Optional[uuid.UUID]:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_key(*parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bounded_string(value: Any, limit: int = _MAX_STRING_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _sanitize_value(
    value: Any,
    key: Optional[str] = None,
    *,
    redact_secrets: bool = True,
) -> Any:
    if key and redact_secrets:
        redacted = redact_secret_value(key, value)
        if redacted != value:
            return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if redact_secrets:
            _, rejection_reason = classify_sensitivity(value)
            if rejection_reason:
                return "[REDACTED_SECRET]"
        return _bounded_string(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= _MAX_DICT_ITEMS:
                result["__truncated__"] = True
                break
            clean_key = _bounded_string(child_key, 128)
            result[clean_key] = _sanitize_value(
                child_value,
                clean_key,
                redact_secrets=redact_secrets,
            )
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized = [
            _sanitize_value(item, redact_secrets=redact_secrets)
            for item in items[:_MAX_LIST_ITEMS]
        ]
        if len(items) > _MAX_LIST_ITEMS:
            sanitized.append({"__truncated__": True})
        return sanitized
    return _bounded_string(value)


def sanitize_provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    sanitized = _sanitize_value(dict(value or {}))
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= _MAX_PROVENANCE_BYTES:
        return sanitized
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "top_level_keys": sorted(str(key)[:128] for key in sanitized.keys())[:64],
    }


def _assert_private_skill_content(value: Any) -> None:
    try:
        assert_no_secret_like_skill_content(value)
    except SkillContentPrivacyError:
        # Do not include the rejected value or field path in an API/DB error.
        raise SkillLearningValidationError(
            "proposed Skill content contains secret-like material"
        ) from None


def _assert_proposal_content_private(
    proposal: SkillProposal,
    *,
    include_proposed: bool = True,
    include_base: bool = True,
) -> None:
    # Keep this guard tolerant of lightweight row doubles used by callers and
    # tests; persisted SkillProposal rows expose both attributes.
    proposed_content = getattr(proposal, "proposed_content", None)
    base_snapshot = getattr(proposal, "base_snapshot", None)
    if include_proposed and proposed_content:
        _assert_private_skill_content(proposed_content)
    if include_base and base_snapshot:
        _assert_private_skill_content(base_snapshot)


def _normalize_skill_payload(
    *,
    name: str,
    payload: Mapping[str, Any],
    target_scope: str,
    reject_secret_fields: bool = False,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not _SKILL_NAME_RE.fullmatch(clean_name):
        raise SkillLearningValidationError("invalid skill name")

    prompt = str(payload.get("prompt_template") or "")
    if not prompt.strip():
        raise SkillLearningValidationError("prompt_template is required")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise SkillLearningValidationError("prompt_template is too large")
    _, rejection_reason = classify_sensitivity(prompt)
    if rejection_reason:
        raise SkillLearningValidationError(
            "proposed Skill content contains secret-like material"
        )

    trigger_mode = str(payload.get("trigger_mode") or "both").strip().lower()
    if trigger_mode not in {"manual", "auto", "both"}:
        trigger_mode = "both"

    def clean_list(key: str, limit: int = 64) -> list[str]:
        """Bound list content before privacy validation/persistence."""
        raw = payload.get(key) or []
        if not isinstance(raw, list):
            raise SkillLearningValidationError(f"{key} must be a list")
        result: list[str] = []
        seen: set[str] = set()
        for item in raw[:limit]:
            text = _bounded_string(item, 256).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    description = _bounded_string(
        payload.get("description"),
        2_000,
    ).strip()
    aliases = clean_list("aliases")
    bound_tools = clean_list("bound_tools")
    examples = clean_list("examples", 32)
    tags = clean_list("tags", 64)

    parameters = payload.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise SkillLearningValidationError("parameters must be an object")
    if target_scope == "project":
        unsupported = {
            "aliases": payload.get("aliases") or [],
            "examples": payload.get("examples") or [],
            "tags": payload.get("tags") or [],
            "parameters": parameters,
        }
        if any(bool(value) for value in unsupported.values()):
            raise SkillLearningValidationError(
                "project SKILL.md proposals support only description, prompt_template, "
                "trigger_mode, and bound_tools"
            )

    # Bound nested data first, but do not redact it before the fail-closed
    # proposal privacy check; otherwise a credential could silently become
    # persisted "[REDACTED]" proposal content instead of being rejected.
    bounded_parameters = _sanitize_value(
        parameters,
        redact_secrets=False,
    )
    normalized = {
        "name": clean_name,
        "description": description,
        "prompt_template": prompt,
        "trigger_mode": trigger_mode,
        "aliases": aliases,
        "bound_tools": bound_tools,
        "examples": examples,
        "tags": tags,
        "parameters": bounded_parameters,
    }
    if reject_secret_fields:
        # target_name and trigger_mode are controlled identifiers/enums rather
        # than free-form content. All persisted free-form/nested Skill content
        # is inspected here.
        _assert_private_skill_content(
            {
                "description": description,
                "prompt_template": prompt,
                "aliases": aliases,
                "bound_tools": bound_tools,
                "examples": examples,
                "tags": tags,
                "parameters": bounded_parameters,
            }
        )

    # Skill content deliberately uses the narrower credential-key boundary
    # rules rather than field_crypto's general substring-based audit redactor.
    # Value-pattern detection remains fail-closed/redacted as before.
    normalized["parameters"] = redact_secret_like_skill_content(
        bounded_parameters
    )
    return normalized


class SkillLearningService:
    """Durable proposal, history, and usage-receipt authority for Skills."""

    def __init__(
        self,
        *,
        global_skills_dir: Optional[Path] = None,
        workspace_resolver: Optional[Callable[[uuid.UUID], Path]] = None,
    ) -> None:
        if global_skills_dir is None:
            from ..skills.loader import SKILLS_DIR

            global_skills_dir = SKILLS_DIR
        self._global_skills_dir = Path(global_skills_dir)
        if workspace_resolver is None:
            from .project_workspace_cleanup import get_project_workspace_path

            workspace_resolver = get_project_workspace_path
        self._workspace_resolver = workspace_resolver

    @asynccontextmanager
    async def _session(self):
        from ..memory.database import get_database_manager

        session = await get_database_manager().get_session()
        try:
            yield session
        finally:
            await session.close()

    @staticmethod
    async def _require_project_permission(
        session: Any,
        *,
        actor_id: str,
        project_id: str,
        write: bool,
    ) -> Project:
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise SkillLearningForbidden("project access denied")

        # Auth-disabled local mode uses the repository-wide ``default_user``
        # principal as an administrator.  Keep that compatibility without
        # turning arbitrary non-UUID actor strings into a project-existence
        # oracle in authenticated mode.
        local_default_user = str(actor_id) == "default_user"
        actor_uuid = None if local_default_user else _uuid(actor_id)
        if not local_default_user and actor_uuid is None:
            raise SkillLearningForbidden("project access denied")

        project = await session.get(Project, project_uuid)
        if project is None or project.deleted_at is not None:
            raise SkillLearningNotFound("project not found")
        if local_default_user:
            return project

        assert actor_uuid is not None
        user = await session.get(User, actor_uuid)
        if user is not None and str(getattr(user, "role", "")).casefold() == "admin":
            return project
        if project.owner_id == actor_uuid:
            return project
        member = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_uuid,
                ProjectMember.user_id == actor_uuid,
            )
        )
        if member is None:
            raise SkillLearningForbidden("project access denied")
        if not write:
            permissions = member.permissions if isinstance(member.permissions, dict) else {}
            if member.role in {"owner", "admin"} or permissions.get("read") is True:
                return project
            raise SkillLearningForbidden("project read denied")
        permissions = member.permissions if isinstance(member.permissions, dict) else {}
        if (
            member.role in {"owner", "admin"}
            or permissions.get("write") is True
            or permissions.get("manage_settings") is True
        ):
            return project
        raise SkillLearningForbidden("project write/manage permission required")

    def _target_path(
        self,
        *,
        target_scope: str,
        target_name: str,
        project_id: Optional[str],
    ) -> tuple[Path, str]:
        scope = str(target_scope or "").strip().lower()
        name = str(target_name or "").strip()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise SkillLearningValidationError("invalid skill name")
        if scope == "global":
            return (
                self._global_skills_dir / f"{name}.yaml",
                f"config/skills/{name}.yaml",
            )
        if scope != "project" or not project_id:
            raise SkillLearningValidationError("project_id is required for project skill")
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise SkillLearningValidationError("invalid project_id")
        workspace = Path(self._workspace_resolver(project_uuid)).resolve()
        root = (workspace / ".agents" / "skills").resolve()
        path = (root / name / "SKILL.md").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SkillLearningValidationError("skill target escapes project workspace") from exc
        return path, f".agents/skills/{name}/SKILL.md"

    def snapshot_target(
        self,
        *,
        target_scope: str,
        target_name: str,
        project_id: Optional[str] = None,
    ) -> SkillTargetSnapshot:
        path, canonical_path = self._target_path(
            target_scope=target_scope,
            target_name=target_name,
            project_id=project_id,
        )
        try:
            text = read_stable_skill_text(path)
        except SkillTargetTransitionPending as exc:
            raise SkillLearningConflict(
                "Skill target has an unfinished transition"
            ) from exc
        if text is None:
            return SkillTargetSnapshot(
                scope=target_scope,
                name=target_name,
                project_id=project_id,
                path=path,
                canonical_path=canonical_path,
                exists=False,
                text=None,
                payload=None,
                sha256=None,
                version=None,
            )
        sha256 = _digest_text(text)
        if target_scope == "global":
            from ..skills.loader import parse_skill_yaml_text

            skill = parse_skill_yaml_text(text, path)
            if skill is None:
                raise SkillLearningValidationError("canonical global Skill cannot be parsed")
            payload = skill.to_dict()
        else:
            from .skill_recording_service import parse_skill_markdown

            parsed = parse_skill_markdown(text)
            payload = {
                "name": parsed.get("name") or target_name,
                "description": parsed.get("description") or "",
                "prompt_template": parsed.get("body") or "",
                "trigger_mode": parsed.get("trigger_mode") or "both",
                "aliases": [],
                "bound_tools": parsed.get("bound_tools") or [],
                "examples": [],
                "tags": [],
                "parameters": {},
            }
        payload = _normalize_skill_payload(
            name=target_name,
            payload=payload,
            target_scope=target_scope,
        )
        return SkillTargetSnapshot(
            scope=target_scope,
            name=target_name,
            project_id=project_id,
            path=path,
            canonical_path=canonical_path,
            exists=True,
            text=text,
            payload=payload,
            sha256=sha256,
            version=f"sha256:{sha256}",
        )

    def snapshot_for_skill(
        self,
        skill: Any,
        *,
        project_id: Optional[str],
    ) -> SkillTargetSnapshot:
        source_path = str(getattr(skill, "source_path", "") or "")
        source_norm = source_path.replace("\\", "/")
        scope = "project" if "/.agents/skills/" in f"/{source_norm}" else "global"
        effective_project_id = project_id if scope == "project" else None
        snapshot = self.snapshot_target(
            target_scope=scope,
            target_name=str(getattr(skill, "name", "") or ""),
            project_id=effective_project_id,
        )
        if not snapshot.exists:
            raise SkillLearningValidationError("canonical Skill file is unavailable")
        return snapshot

    def prepare_usage_snapshot(
        self,
        skill: Any,
        *,
        project_id: Optional[str],
    ) -> SkillTargetSnapshot:
        """Freeze the exact canonical Skill version that will be rendered."""
        return self.snapshot_for_skill(
            skill,
            project_id=project_id,
        )

    async def _validate_usage_context(
        self,
        session: Any,
        *,
        actor_id: str,
        project_id: Optional[str],
        session_id: Optional[str],
        message_id: Optional[str],
        agent_run_id: Optional[str],
        require_masking_source: bool = False,
    ) -> None:
        if not message_id and not agent_run_id:
            raise SkillLearningValidationError(
                "actual-use receipt requires a trusted message or AgentRun"
            )
        message_uuid = _uuid(message_id)
        session_uuid = _uuid(session_id)
        project_uuid = _uuid(project_id)
        run_uuid = _uuid(agent_run_id)
        if message_id and message_uuid is None:
            raise SkillLearningValidationError("invalid message_id")
        if agent_run_id and run_uuid is None:
            raise SkillLearningValidationError("invalid agent_run_id")

        if message_uuid is not None:
            message = await session.get(ConversationMessage, message_uuid)
            if (
                message is None
                or message.role != "user"
                or str(message.sender_type or "") != "user"
                or not message.sender_id
                or str(message.sender_id) != str(actor_id)
                or (session_uuid is not None and message.session_id != session_uuid)
            ):
                raise SkillLearningForbidden("untrusted Skill receipt message context")
            if require_masking_source:
                # The masking route marks the canonical raw user row before
                # invoking this service.  Verify that structural marker here
                # without reading/copying message content; user-authored text
                # that merely resembles the marker is never authoritative.
                from .privacy_masking_projection import is_privacy_masking_source

                if not is_privacy_masking_source(message):
                    raise SkillLearningForbidden(
                        "untrusted masking source message context"
                    )

        if run_uuid is not None:
            run = await session.get(AgentRun, run_uuid)
            if run is None or str(run.user_id or "") != str(actor_id):
                raise SkillLearningForbidden("untrusted Skill receipt AgentRun context")
            if session_uuid is not None and run.session_id != session_uuid:
                raise SkillLearningForbidden("AgentRun/session mismatch")
            if project_uuid is not None and run.project_id != project_uuid:
                raise SkillLearningForbidden("AgentRun/project mismatch")
            if message_uuid is not None and run.trigger_message_id != message_uuid:
                raise SkillLearningForbidden("AgentRun/message mismatch")

    async def record_usage(
        self,
        *,
        actor_id: str,
        skill: Any,
        invocation_path: str,
        outcome: str,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
        extra_provenance: Optional[Mapping[str, Any]] = None,
        rendered_snapshot: Optional[SkillTargetSnapshot] = None,
    ) -> Optional[dict[str, Any]]:
        actor = str(actor_id or "").strip()
        if not actor:
            return None
        invocation = str(invocation_path or "").strip().lower()
        if invocation not in {"invoke_skill", "slash", "chain", "heartbeat"}:
            raise SkillLearningValidationError("unsupported Skill invocation path")
        result_outcome = str(outcome or "").strip().lower()
        if result_outcome not in {"success", "error"}:
            raise SkillLearningValidationError("invalid Skill receipt outcome")

        # A usage receipt is actual-use evidence only when trusted provenance
        # identifies either the originating message or AgentRun.  Reject this
        # before reading the canonical Skill snapshot or opening a DB session.
        if not message_id and not agent_run_id:
            raise SkillLearningValidationError(
                "actual-use receipt requires a trusted message or AgentRun"
            )

        if rendered_snapshot is None:
            snapshot = self.snapshot_for_skill(skill, project_id=project_id)
        else:
            snapshot = rendered_snapshot
            if not isinstance(snapshot, SkillTargetSnapshot):
                raise SkillLearningValidationError(
                    "rendered Skill snapshot is invalid"
                )
            if (
                not snapshot.exists
                or snapshot.text is None
                or snapshot.payload is None
                or not snapshot.sha256
                or not snapshot.version
            ):
                raise SkillLearningValidationError(
                    "rendered Skill snapshot is incomplete"
                )
            if snapshot.name != str(getattr(skill, "name", "") or ""):
                raise SkillLearningValidationError(
                    "rendered Skill snapshot does not match Skill identity"
                )
            if _digest_text(snapshot.text) != snapshot.sha256:
                raise SkillLearningValidationError(
                    "rendered Skill snapshot hash mismatch"
                )
            if snapshot.version != f"sha256:{snapshot.sha256}":
                raise SkillLearningValidationError(
                    "rendered Skill snapshot version mismatch"
                )
            if snapshot.scope == "project":
                if (
                    not project_id
                    or not snapshot.project_id
                    or str(snapshot.project_id) != str(project_id)
                ):
                    raise SkillLearningValidationError(
                        "rendered project Skill snapshot scope mismatch"
                    )
            elif snapshot.scope != "global":
                raise SkillLearningValidationError(
                    "rendered Skill snapshot scope is invalid"
                )

        if snapshot.scope == "project" and not project_id:
            raise SkillLearningValidationError("project Skill receipt lacks project_id")

        key = _stable_key(
            "skill-usage",
            actor,
            invocation,
            agent_run_id,
            session_id,
            message_id,
            tool_call_id,
            client_message_id,
            snapshot.scope,
            snapshot.project_id,
            snapshot.name,
            snapshot.sha256,
            result_outcome,
        )
        provenance = sanitize_provenance(
            {
                "turn": {
                    "user_id": actor,
                    "project_id": project_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "agent_run_id": agent_run_id,
                    "tool_call_id": tool_call_id,
                    "client_message_id": client_message_id,
                },
                "skill": {
                    "name": snapshot.name,
                    "scope": snapshot.scope,
                    "path": snapshot.canonical_path,
                    "hash": snapshot.sha256,
                    "version": snapshot.version,
                },
                "extra": dict(extra_provenance or {}),
            }
        )

        async with self._session() as session:
            await self._validate_usage_context(
                session,
                actor_id=actor,
                project_id=(snapshot.project_id if snapshot.scope == "project" else None),
                session_id=session_id,
                message_id=message_id,
                agent_run_id=agent_run_id,
            )
            existing = await session.scalar(
                select(SkillUsageReceipt).where(
                    SkillUsageReceipt.idempotency_key == key
                )
            )
            if existing is not None:
                return existing.to_dict()
            receipt = SkillUsageReceipt(
                id=uuid.uuid4(),
                user_id=actor,
                project_id=_uuid(snapshot.project_id),
                session_id=_uuid(session_id),
                message_id=_uuid(message_id),
                agent_run_id=_uuid(agent_run_id),
                tool_call_id=(str(tool_call_id)[:160] if tool_call_id else None),
                invocation_path=invocation,
                outcome=result_outcome,
                skill_name=snapshot.name,
                skill_scope=snapshot.scope,
                skill_path=snapshot.canonical_path,
                skill_hash=str(snapshot.sha256),
                skill_version=str(snapshot.version),
                idempotency_key=key,
                provenance=provenance,
                created_at=datetime.utcnow(),
            )
            session.add(receipt)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SkillUsageReceipt).where(
                        SkillUsageReceipt.idempotency_key == key
                    )
                )
                if existing is None:
                    raise
                return existing.to_dict()
            await session.refresh(receipt)
            return receipt.to_dict()

    async def record_builtin_masking_usage(
        self,
        *,
        actor_id: str,
        session_id: str,
        message_id: str,
        outcome: str,
        agent_run_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        # These are accepted for call-site symmetry with ``record_usage`` but
        # are intentionally never persisted.  A masking receipt is an audit
        # fact about the trusted source turn, not an artifact/alias ledger.
        project_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
        extra_provenance: Optional[Mapping[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Record one trusted built-in ``/masking`` operation.

        Unlike a normal Skill invocation, masking has no canonical file-backed
        target or render snapshot.  The receipt therefore uses a constant
        synthetic Skill identity in the existing ``global`` scope and stores
        only the actor/source-message/session (plus optional AgentRun) IDs and
        outcome.  In particular, source content, source paths/filenames,
        detected entities, and reversible alias maps are deliberately not
        accepted into the durable payload.

        ``ConversationMessage`` validation is intentionally delegated to the
        same trusted-context validator used by normal Skill receipts.  This
        method never creates a message/AgentRun and does not weaken that
        validator; a source message and session are mandatory for masking.
        Retries converge on the supplied idempotency key (or a deterministic
        key derived only from trusted IDs and the outcome).
        """
        del tool_call_id, client_message_id, extra_provenance

        actor = str(actor_id or "").strip()
        if not actor:
            return None

        result_outcome = str(outcome or "").strip().lower()
        if result_outcome not in {"success", "error"}:
            raise SkillLearningValidationError("invalid Skill receipt outcome")

        # A built-in receipt remains global by construction.  Silently
        # accepting a project identity here would produce a global row with a
        # project foreign key, violating the existing global/project ACL
        # contract used by listing and correction resolution.
        if project_id not in (None, ""):
            raise SkillLearningValidationError(
                "built-in masking receipt cannot use project scope"
            )

        session_uuid = _uuid(session_id)
        message_uuid = _uuid(message_id)
        run_uuid = _uuid(agent_run_id)
        if session_uuid is None:
            raise SkillLearningValidationError("invalid session_id")
        if message_uuid is None:
            raise SkillLearningValidationError("invalid message_id")
        if agent_run_id and run_uuid is None:
            raise SkillLearningValidationError("invalid agent_run_id")

        # Validate before opening the persistence transaction and, most
        # importantly, before an idempotency hit can return a row.  The same
        # trusted ConversationMessage/AgentRun checks as normal Skill usage
        # are thereby retained; no generic trust bypass is introduced.
        async with self._session() as session:
            await self._validate_usage_context(
                session,
                actor_id=actor,
                project_id=None,
                session_id=str(session_uuid),
                message_id=str(message_uuid),
                agent_run_id=str(run_uuid) if run_uuid is not None else None,
                require_masking_source=True,
            )

            supplied_key = str(idempotency_key or "").strip()
            if supplied_key:
                if len(supplied_key) > 128:
                    raise SkillLearningValidationError(
                        "idempotency_key is too long"
                    )
                key = supplied_key
            else:
                key = _stable_key(
                    "builtin-masking-usage",
                    actor,
                    session_uuid,
                    message_uuid,
                    run_uuid,
                    result_outcome,
                )

            existing = await session.scalar(
                select(SkillUsageReceipt).where(
                    SkillUsageReceipt.idempotency_key == key
                )
            )
            if existing is not None:
                # An explicitly supplied idempotency key is still scoped to
                # its actor.  Never return another user's receipt on a key
                # collision, even if a caller bypassed the derived key.
                if str(existing.user_id) != actor:
                    raise SkillLearningForbidden(
                        "usage receipt idempotency key belongs to another actor"
                    )
                if (
                    existing.invocation_path != MASKING_INVOCATION_PATH
                    or existing.session_id != session_uuid
                    or existing.message_id != message_uuid
                    or existing.agent_run_id != run_uuid
                    or existing.outcome != result_outcome
                ):
                    raise SkillLearningConflict(
                        "masking idempotency key identifies another operation"
                    )
                return existing.to_dict()

            # Keep provenance intentionally tiny and fixed-schema.  Do not
            # merge caller-provided metadata: that is where raw text,
            # filenames, entities, and alias maps could otherwise leak.
            provenance_payload: dict[str, Any] = {
                "kind": "builtin_masking",
                "source": {
                    "user_id": actor,
                    "session_id": str(session_uuid),
                    "message_id": str(message_uuid),
                },
                "outcome": result_outcome,
            }
            if run_uuid is not None:
                provenance_payload["source"]["agent_run_id"] = str(run_uuid)

            receipt = SkillUsageReceipt(
                id=uuid.uuid4(),
                user_id=actor,
                # Built-in masking is intentionally not a project Skill.  The
                # source project, if any, remains discoverable through the
                # trusted message/session without copying project-scoped
                # artifact metadata into this receipt.
                project_id=None,
                session_id=session_uuid,
                message_id=message_uuid,
                agent_run_id=run_uuid,
                tool_call_id=None,
                invocation_path=MASKING_INVOCATION_PATH,
                outcome=result_outcome,
                skill_name=MASKING_RECEIPT_SKILL_NAME,
                skill_scope="global",
                skill_path=MASKING_RECEIPT_SKILL_PATH,
                skill_hash=MASKING_RECEIPT_SKILL_HASH,
                skill_version=MASKING_RECEIPT_SKILL_VERSION,
                idempotency_key=key,
                provenance=sanitize_provenance(provenance_payload),
                created_at=datetime.utcnow(),
            )
            session.add(receipt)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SkillUsageReceipt).where(
                        SkillUsageReceipt.idempotency_key == key
                    )
                )
                if existing is None:
                    raise
                if str(existing.user_id) != actor:
                    raise SkillLearningForbidden(
                        "usage receipt idempotency key belongs to another actor"
                    )
                if (
                    existing.invocation_path != MASKING_INVOCATION_PATH
                    or existing.session_id != session_uuid
                    or existing.message_id != message_uuid
                    or existing.agent_run_id != run_uuid
                    or existing.outcome != result_outcome
                ):
                    raise SkillLearningConflict(
                        "masking idempotency key identifies another operation"
                    )
                return existing.to_dict()
            await session.refresh(receipt)
            return receipt.to_dict()

    # Short alias for callers that do not need to spell out the built-in
    # distinction.  Keep one implementation so both names retain identical
    # trust and privacy semantics.
    record_masking_usage = record_builtin_masking_usage

    async def get_receipt(
        self,
        receipt_id: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        receipt_uuid = _uuid(receipt_id)
        if receipt_uuid is None:
            raise SkillLearningNotFound("usage receipt not found")
        async with self._session() as session:
            receipt = await session.get(SkillUsageReceipt, receipt_uuid)
            if receipt is None or str(receipt.user_id) != str(actor_id):
                raise SkillLearningNotFound("usage receipt not found")
            if receipt.project_id is not None:
                await self._require_project_permission(
                    session,
                    actor_id=str(actor_id),
                    project_id=str(receipt.project_id),
                    write=False,
                )
            return receipt.to_dict()

    async def list_receipts(
        self,
        *,
        actor_id: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        skill_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List only owner-visible actual-use receipts with current Project ACL."""
        actor = str(actor_id or "").strip()
        if not actor:
            raise SkillLearningForbidden("usage receipt access denied")

        session_uuid = _uuid(session_id)
        if session_id and session_uuid is None:
            raise SkillLearningValidationError("invalid session_id")

        project_uuid = _uuid(project_id)
        if project_id and project_uuid is None:
            raise SkillLearningValidationError("invalid project_id")

        canonical_skill_name = str(skill_name or "").strip() or None
        if canonical_skill_name and not _SKILL_NAME_RE.fullmatch(canonical_skill_name):
            raise SkillLearningValidationError("invalid skill name")

        safe_limit = max(1, min(int(limit), 200))
        async with self._session() as session:
            statement = select(SkillUsageReceipt).where(
                SkillUsageReceipt.user_id == actor
            )
            if session_uuid is not None:
                statement = statement.where(
                    SkillUsageReceipt.session_id == session_uuid
                )
            if project_uuid is not None:
                await self._require_project_permission(
                    session,
                    actor_id=actor,
                    project_id=str(project_uuid),
                    write=False,
                )
                statement = statement.where(
                    SkillUsageReceipt.project_id == project_uuid
                )
            if canonical_skill_name is not None:
                statement = statement.where(
                    SkillUsageReceipt.skill_name == canonical_skill_name
                )

            rows = list(
                (
                    await session.execute(
                        statement.order_by(
                            SkillUsageReceipt.created_at.desc(),
                            SkillUsageReceipt.id.desc(),
                        ).limit(safe_limit)
                    )
                )
                .scalars()
                .all()
            )

            visible: list[dict[str, Any]] = []
            for row in rows:
                if row.skill_scope == "project":
                    if row.project_id is None:
                        # Project FK is SET NULL on deletion; a lost Project
                        # identity must never make a former project receipt
                        # look like a global receipt.
                        continue
                    if project_uuid is None:
                        try:
                            await self._require_project_permission(
                                session,
                                actor_id=actor,
                                project_id=str(row.project_id),
                                write=False,
                            )
                        except (SkillLearningForbidden, SkillLearningNotFound):
                            continue
                visible.append(row.to_dict())
            return visible

    async def resolve_recent_receipt_for_correction(
        self,
        *,
        actor_id: str,
        session_id: str,
        project_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Resolve one unambiguous latest actual operation in this actor/session.

        Auto-resolution is intentionally stricter than the general listing API:
        - no cross-user lookup;
        - no cross-session lookup;
        - first find the actual latest operation across every Skill scope in
          this actor/session;
        - only then require that latest operation to match the current exact
          Project/global correction scope;
        - if the latest AgentRun/message produced multiple Skill receipts, the
          target is ambiguous and no receipt is selected.

        This ordering is deliberate: an older in-scope receipt must never win
        merely because a newer actual Skill use happened in another scope.
        """
        actor = str(actor_id or "").strip()
        session_uuid = _uuid(session_id)
        if not actor or session_uuid is None:
            return None

        project_uuid = _uuid(project_id)
        if project_id and project_uuid is None:
            return None

        async with self._session() as session:
            if project_uuid is not None:
                await self._require_project_permission(
                    session,
                    actor_id=actor,
                    project_id=str(project_uuid),
                    write=False,
                )

            # Do not scope-filter here.  Scope is checked only after the latest
            # actual operation in the actor/session has been established.
            statement = select(SkillUsageReceipt).where(
                SkillUsageReceipt.user_id == actor,
                SkillUsageReceipt.session_id == session_uuid,
            )

            rows = list(
                (
                    await session.execute(
                        statement.order_by(
                            SkillUsageReceipt.created_at.desc(),
                            SkillUsageReceipt.id.desc(),
                        ).limit(2)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return None

            latest = rows[0]
            latest_operation = (
                ("run", str(latest.agent_run_id))
                if latest.agent_run_id is not None
                else ("message", str(latest.message_id))
                if latest.message_id is not None
                else None
            )
            if latest_operation is None:
                return None

            # A masking receipt is evidence of a one-way built-in projection,
            # not evidence for a file-backed Skill correction.  Treat it as
            # the latest operation boundary rather than falling back to an
            # older Skill receipt.
            if _is_builtin_masking_receipt(latest):
                return None

            if len(rows) > 1:
                previous = rows[1]
                previous_operation = (
                    ("run", str(previous.agent_run_id))
                    if previous.agent_run_id is not None
                    else ("message", str(previous.message_id))
                    if previous.message_id is not None
                    else None
                )
                if (
                    previous_operation == latest_operation
                    or previous.created_at == latest.created_at
                ):
                    return None

            if project_uuid is not None:
                if (
                    latest.skill_scope != "project"
                    or latest.project_id != project_uuid
                ):
                    return None
            elif (
                latest.skill_scope != "global"
                or latest.project_id is not None
            ):
                return None

            return latest.to_dict()

    async def _next_history_sequence(self, session: Any, proposal_id: uuid.UUID) -> int:
        value = await session.scalar(
            select(func.max(SkillProposalHistory.sequence)).where(
                SkillProposalHistory.proposal_id == proposal_id
            )
        )
        return int(value or 0) + 1

    async def _append_history(
        self,
        session: Any,
        *,
        proposal: SkillProposal,
        actor_id: str,
        event: str,
        from_status: Optional[str],
        to_status: Optional[str],
        observed_hash: Optional[str] = None,
        result_hash: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        session.add(
            SkillProposalHistory(
                id=uuid.uuid4(),
                proposal_id=proposal.id,
                sequence=await self._next_history_sequence(session, proposal.id),
                event=event,
                actor_user_id=str(actor_id),
                from_status=from_status,
                to_status=to_status,
                observed_hash=observed_hash,
                result_hash=result_hash,
                details=sanitize_provenance(details or {}),
                created_at=datetime.utcnow(),
            )
        )

    async def _authorize_proposal(
        self,
        session: Any,
        *,
        proposal: SkillProposal,
        actor_id: str,
        write: bool,
    ) -> None:
        if str(proposal.user_id) != str(actor_id):
            raise SkillLearningNotFound("Skill proposal not found")
        if proposal.target_scope == "project":
            if proposal.project_id is None:
                raise SkillLearningConflict("project proposal lost project identity")
            await self._require_project_permission(
                session,
                actor_id=str(actor_id),
                project_id=str(proposal.project_id),
                write=write,
            )

    async def _load_proposal(
        self,
        session: Any,
        *,
        proposal_id: str,
        for_update: bool,
    ) -> SkillProposal:
        proposal_uuid = _uuid(proposal_id)
        if proposal_uuid is None:
            raise SkillLearningNotFound("Skill proposal not found")
        statement = select(SkillProposal).where(SkillProposal.id == proposal_uuid)
        if for_update:
            statement = statement.with_for_update()
        proposal = (await session.execute(statement)).scalar_one_or_none()
        if proposal is None:
            raise SkillLearningNotFound("Skill proposal not found")
        return proposal

    async def create_proposal(
        self,
        *,
        actor_id: str,
        operation: str,
        target_scope: str,
        target_name: str,
        proposed_content: Mapping[str, Any],
        project_id: Optional[str] = None,
        reason_type: str = "manual",
        receipt_id: Optional[str] = None,
        evidence: Optional[list[dict[str, Any]]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        actor = str(actor_id or "").strip()
        op = str(operation or "").strip().lower()
        scope = str(target_scope or "").strip().lower()
        reason = str(reason_type or "manual").strip().lower()
        receipt_ref = str(receipt_id or "").strip()
        receipt_uuid = _uuid(receipt_ref) if receipt_ref else None
        if op not in {"create", "update"}:
            raise SkillLearningValidationError("operation must be create or update")
        if scope not in {"global", "project"}:
            raise SkillLearningValidationError("invalid target_scope")
        if reason not in {"manual", "correction", "procedure"}:
            raise SkillLearningValidationError("invalid reason_type")
        if receipt_ref and receipt_uuid is None:
            raise SkillLearningValidationError("invalid receipt_id")
        if reason == "correction" and receipt_uuid is None:
            raise SkillLearningValidationError(
                "evidence-backed Skill correction requires an actual usage receipt"
            )
        if scope == "project" and not project_id:
            raise SkillLearningValidationError("project_id is required")
        if scope == "global":
            project_id = None

        # Project targets are authorization-sensitive filesystem resources.
        # Establish canonical write access before resolving the workspace or
        # parsing any Skill file.  A second check in the persistence session
        # below closes the revocation window before durable proposal creation.
        if scope == "project":
            async with self._session() as authorization_session:
                await self._require_project_permission(
                    authorization_session,
                    actor_id=actor,
                    project_id=str(project_id),
                    write=True,
                )

        snapshot = self.snapshot_target(
            target_scope=scope,
            target_name=target_name,
            project_id=project_id,
        )
        if op == "create" and snapshot.exists:
            raise SkillLearningConflict("Skill target already exists")
        if op == "update" and not snapshot.exists:
            raise SkillLearningNotFound("Skill target does not exist")

        safe_base_snapshot: Optional[dict[str, Any]] = None
        if snapshot.payload is not None:
            safe_base_snapshot = _normalize_skill_payload(
                name=str(target_name),
                payload=snapshot.payload,
                target_scope=scope,
                reject_secret_fields=True,
            )

        merged: dict[str, Any]
        if op == "update":
            merged = dict(safe_base_snapshot or {})
            merged.update(dict(proposed_content or {}))
        else:
            merged = dict(proposed_content or {})
        merged["name"] = str(target_name)
        normalized = _normalize_skill_payload(
            name=str(target_name),
            payload=merged,
            target_scope=scope,
            reject_secret_fields=True,
        )

        provenance_payload = {
            "source": dict(provenance or {}),
            "evidence": list(evidence or [])[:32],
        }
        effective_key = str(idempotency_key or "").strip() or _stable_key(
            "skill-proposal",
            actor,
            op,
            reason,
            scope,
            project_id,
            target_name,
            snapshot.sha256,
            receipt_ref,
            json.dumps(normalized, sort_keys=True, ensure_ascii=False),
        )

        async with self._session() as session:
            if scope == "project":
                await self._require_project_permission(
                    session,
                    actor_id=actor,
                    project_id=str(project_id),
                    write=True,
                )
            existing = await session.scalar(
                select(SkillProposal).where(
                    SkillProposal.idempotency_key == effective_key
                )
            )
            if existing is not None:
                await self._authorize_proposal(
                    session,
                    proposal=existing,
                    actor_id=actor,
                    write=False,
                )
                _assert_proposal_content_private(existing)
                return existing.to_dict(include_history=True)

            receipt = None
            if receipt_uuid is not None:
                receipt = await session.get(SkillUsageReceipt, receipt_uuid)
                if receipt is None or str(receipt.user_id) != actor:
                    raise SkillLearningNotFound("usage receipt not found")
                if (
                    receipt.skill_scope != scope
                    or receipt.skill_name != str(target_name)
                    or str(receipt.project_id or "") != str(_uuid(project_id) or "")
                ):
                    raise SkillLearningValidationError(
                        "usage receipt does not identify the proposal target"
                    )
                if op == "update" and receipt.skill_hash != snapshot.sha256:
                    raise SkillLearningConflict(
                        "usage receipt refers to an older Skill version; invoke the current Skill first"
                    )
            elif reason == "correction":
                raise SkillLearningValidationError(
                    "evidence-backed Skill correction requires an actual usage receipt"
                )

            proposal = SkillProposal(
                id=uuid.uuid4(),
                user_id=actor,
                project_id=_uuid(project_id),
                receipt_id=receipt_uuid,
                operation=op,
                reason_type=reason,
                target_scope=scope,
                target_name=str(target_name),
                target_path=snapshot.canonical_path,
                status="pending",
                proposed_content=normalized,
                base_snapshot=safe_base_snapshot,
                base_text=snapshot.text,
                base_hash=snapshot.sha256,
                base_version=snapshot.version,
                idempotency_key=effective_key,
                provenance=sanitize_provenance(provenance_payload),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(proposal)
            await session.flush()
            await self._append_history(
                session,
                proposal=proposal,
                actor_id=actor,
                event="created",
                from_status=None,
                to_status="pending",
                observed_hash=snapshot.sha256,
                details={
                    "reason_type": reason,
                    "receipt_id": receipt_ref or None,
                    "target_path": snapshot.canonical_path,
                },
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SkillProposal).where(
                        SkillProposal.idempotency_key == effective_key
                    )
                )
                if existing is None:
                    raise
                await self._authorize_proposal(
                    session,
                    proposal=existing,
                    actor_id=actor,
                    write=False,
                )
                _assert_proposal_content_private(existing)
                return existing.to_dict(include_history=True)
            await session.refresh(proposal)
            return proposal.to_dict(include_history=True)

    async def create_correction_from_receipt(
        self,
        *,
        actor_id: str,
        receipt_id: str,
        correction_text: str,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        correction = str(correction_text or "").strip()
        if not correction:
            raise SkillLearningValidationError("correction text is required")
        receipt = await self.get_receipt(receipt_id, actor_id=str(actor_id))
        snapshot = self.snapshot_target(
            target_scope=str(receipt["skill_scope"]),
            target_name=str(receipt["skill_name"]),
            project_id=receipt.get("project_id"),
        )
        if not snapshot.exists or snapshot.sha256 != receipt.get("skill_hash"):
            raise SkillLearningConflict(
                "usage receipt is not for the current canonical Skill version"
            )
        proposed = dict(snapshot.payload or {})
        base_prompt = str(proposed.get("prompt_template") or "").rstrip()
        proposed["prompt_template"] = (
            base_prompt
            + "\n\n# Evidence-backed correction\n"
            + correction
            + "\n"
        )
        return await self.create_proposal(
            actor_id=str(actor_id),
            operation="update",
            target_scope=str(receipt["skill_scope"]),
            target_name=str(receipt["skill_name"]),
            project_id=receipt.get("project_id"),
            reason_type="correction",
            proposed_content=proposed,
            receipt_id=str(receipt_id),
            evidence=[{"type": "skill_usage_receipt", "id": str(receipt_id)}],
            provenance=provenance,
        )

    async def get_proposal(
        self,
        proposal_id: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        async with self._session() as session:
            proposal = await self._load_proposal(
                session,
                proposal_id=proposal_id,
                for_update=False,
            )
            await self._authorize_proposal(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                write=False,
            )
            _assert_proposal_content_private(proposal)
            return proposal.to_dict(include_history=True)

    async def list_proposals(
        self,
        *,
        actor_id: str,
        status: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        async with self._session() as session:
            statement = select(SkillProposal).where(
                SkillProposal.user_id == str(actor_id)
            )
            if status:
                statement = statement.where(SkillProposal.status == str(status))
            if project_id:
                await self._require_project_permission(
                    session,
                    actor_id=str(actor_id),
                    project_id=str(project_id),
                    write=False,
                )
                statement = statement.where(
                    SkillProposal.project_id == _uuid(project_id)
                )
            rows = list(
                (
                    await session.execute(
                        statement.order_by(SkillProposal.created_at.desc()).limit(safe_limit)
                    )
                )
                .scalars()
                .all()
            )

            visible: list[dict[str, Any]] = []
            for row in rows:
                if row.target_scope == "project":
                    # ``project_id`` is SET NULL when a Project is deleted.
                    # Never serialize a project proposal until current read ACL
                    # has been revalidated, because serialization exposes the
                    # proposal body, base snapshot, and target path.
                    if row.project_id is None:
                        continue
                    try:
                        await self._require_project_permission(
                            session,
                            actor_id=str(actor_id),
                            project_id=str(row.project_id),
                            write=False,
                        )
                    except (SkillLearningForbidden, SkillLearningNotFound):
                        continue
                try:
                    _assert_proposal_content_private(row)
                except SkillLearningValidationError:
                    # Legacy unsafe rows are never serialized into list output.
                    continue
                visible.append(row.to_dict(include_history=False))
            return visible

    async def revise_proposal(
        self,
        proposal_id: str,
        *,
        actor_id: str,
        proposed_content: Mapping[str, Any],
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        async with self._session() as session:
            proposal = await self._load_proposal(
                session,
                proposal_id=proposal_id,
                for_update=True,
            )
            await self._authorize_proposal(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                write=True,
            )
            _assert_proposal_content_private(proposal)
            if proposal.status != "pending":
                raise SkillLearningConflict("only pending proposals can be revised")
            snapshot = self.snapshot_target(
                target_scope=proposal.target_scope,
                target_name=proposal.target_name,
                project_id=str(proposal.project_id) if proposal.project_id else None,
            )
            expected = proposal.base_hash
            observed = snapshot.sha256
            create_conflict = proposal.operation == "create" and snapshot.exists
            update_conflict = proposal.operation == "update" and (
                not snapshot.exists or observed != expected
            )
            if create_conflict or update_conflict:
                await self._mark_stale(
                    session,
                    proposal=proposal,
                    actor_id=str(actor_id),
                    observed_hash=observed,
                    reason="target changed before proposal revision",
                )
                await session.commit()
                raise SkillLearningStale(
                    "Skill target changed before proposal revision",
                    proposal.to_dict(include_history=True),
                )

            safe_snapshot_payload: Optional[dict[str, Any]] = None
            if snapshot.payload is not None:
                safe_snapshot_payload = _normalize_skill_payload(
                    name=proposal.target_name,
                    payload=snapshot.payload,
                    target_scope=proposal.target_scope,
                    reject_secret_fields=True,
                )
            merged = (
                dict(safe_snapshot_payload or {})
                if proposal.operation == "update"
                else {}
            )
            merged.update(dict(proposed_content or {}))
            merged["name"] = proposal.target_name
            proposal.proposed_content = _normalize_skill_payload(
                name=proposal.target_name,
                payload=merged,
                target_scope=proposal.target_scope,
                reject_secret_fields=True,
            )
            previous_provenance = proposal.provenance or {}
            proposal.provenance = sanitize_provenance(
                {"previous": previous_provenance, "revision": dict(provenance or {})}
            )
            proposal.updated_at = datetime.utcnow()
            await self._append_history(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                event="updated",
                from_status="pending",
                to_status="pending",
                observed_hash=observed,
                details={"proposal_revision": True},
            )
            await session.commit()
            await session.refresh(proposal)
            return proposal.to_dict(include_history=True)

    async def _mark_stale(
        self,
        session: Any,
        *,
        proposal: SkillProposal,
        actor_id: str,
        observed_hash: Optional[str],
        reason: str,
    ) -> None:
        previous = proposal.status
        proposal.status = "stale"
        proposal.stale_at = datetime.utcnow()
        proposal.updated_at = proposal.stale_at
        await self._append_history(
            session,
            proposal=proposal,
            actor_id=actor_id,
            event="stale",
            from_status=previous,
            to_status="stale",
            observed_hash=observed_hash,
            details={"reason": reason, "expected_hash": proposal.base_hash},
        )

    def _proposal_target_material(
        self,
        proposal: SkillProposal,
    ) -> tuple[Path, str, str, dict[str, Any]]:
        payload = _normalize_skill_payload(
            name=proposal.target_name,
            payload=proposal.proposed_content or {},
            target_scope=proposal.target_scope,
            reject_secret_fields=True,
        )
        path, canonical_path = self._target_path(
            target_scope=proposal.target_scope,
            target_name=proposal.target_name,
            project_id=str(proposal.project_id) if proposal.project_id else None,
        )
        if proposal.target_scope == "global":
            from ..skills.loader import serialize_skill_to_yaml
            from ..skills.models import SkillDefinition, SkillTriggerMode

            skill = SkillDefinition(
                name=proposal.target_name,
                description=payload["description"],
                prompt_template=payload["prompt_template"],
                trigger_mode=SkillTriggerMode(payload["trigger_mode"]),
                aliases=payload["aliases"],
                bound_tools=payload["bound_tools"],
                examples=payload["examples"],
                tags=payload["tags"],
                parameters=payload["parameters"],
            )
            text = serialize_skill_to_yaml(skill)
        else:
            from .skill_recording_service import build_skill_markdown

            text = build_skill_markdown(
                name=proposal.target_name,
                description=payload["description"],
                trigger_mode=payload["trigger_mode"],
                bound_tools=payload["bound_tools"],
                body=payload["prompt_template"],
            )
        return path, canonical_path, text, payload

    def _expected_proposal_hash(self, proposal: SkillProposal) -> Optional[str]:
        try:
            _path, _canonical_path, text, _payload = self._proposal_target_material(
                proposal
            )
        except Exception:
            # Unit/legacy fixtures may deliberately replace _write_target.
            # Production _write_target performs the same validation and cannot
            # publish invalid proposal content.
            return None
        return _digest_text(text)

    def _write_target(self, proposal: SkillProposal) -> SkillTargetSnapshot:
        """Publish proposal content; caller must hold the per-target lock."""
        path, canonical_path, text, payload = self._proposal_target_material(proposal)
        atomic_replace_text(path, text)
        sha256 = _digest_text(text)
        return SkillTargetSnapshot(
            scope=proposal.target_scope,
            name=proposal.target_name,
            project_id=str(proposal.project_id) if proposal.project_id else None,
            path=path,
            canonical_path=canonical_path,
            exists=True,
            text=text,
            payload=payload,
            sha256=sha256,
            version=f"sha256:{sha256}",
        )

    def _validated_base_text_for_restore(
        self,
        proposal: SkillProposal,
        *,
        path: Path,
        text: str,
    ) -> dict[str, Any]:
        """Validate legacy rollback bytes before any publication.

        ``base_snapshot`` cannot be trusted as the sole privacy boundary for
        legacy rows because it may be NULL or may omit unknown YAML/frontmatter
        keys that still exist in ``base_text``. Decode the actual bytes that
        would be restored, recursively inspect the complete decoded structure,
        then validate the canonical Skill projection as well.
        """
        expected_hash = str(getattr(proposal, "base_hash", "") or "").strip()
        observed_hash = _digest_text(text)
        if expected_hash and observed_hash != expected_hash:
            raise SkillLearningConflict(
                "proposal base text hash does not match the recorded base hash"
            )

        if proposal.target_scope == "global":
            try:
                decoded = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                raise SkillLearningValidationError(
                    "stored base Skill cannot be parsed safely"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise SkillLearningValidationError(
                    "stored base Skill must be a mapping"
                )
            _assert_private_skill_content(decoded)

            from ..skills.loader import parse_skill_yaml_text

            skill = parse_skill_yaml_text(text, path)
            if skill is None:
                raise SkillLearningValidationError(
                    "stored base global Skill cannot be parsed"
                )
            payload = skill.to_dict()
        else:
            match = re.fullmatch(
                r"---\r?\n(?P<frontmatter>.*?)\r?\n---(?:\r?\n)?(?P<body>.*)",
                text,
                flags=re.DOTALL,
            )
            if match is None:
                raise SkillLearningValidationError(
                    "stored base project Skill has invalid frontmatter"
                )
            try:
                frontmatter = yaml.safe_load(match.group("frontmatter")) or {}
            except yaml.YAMLError as exc:
                raise SkillLearningValidationError(
                    "stored base project Skill cannot be parsed safely"
                ) from exc
            if not isinstance(frontmatter, Mapping):
                raise SkillLearningValidationError(
                    "stored base project Skill frontmatter must be a mapping"
                )
            _assert_private_skill_content(
                {
                    "frontmatter": dict(frontmatter),
                    "body": match.group("body"),
                }
            )

            from .skill_recording_service import parse_skill_markdown

            parsed = parse_skill_markdown(text)
            payload = {
                "name": parsed.get("name") or proposal.target_name,
                "description": parsed.get("description") or "",
                "prompt_template": parsed.get("body") or "",
                "trigger_mode": parsed.get("trigger_mode") or "both",
                "aliases": [],
                "bound_tools": parsed.get("bound_tools") or [],
                "examples": [],
                "tags": [],
                "parameters": {},
            }

        return _normalize_skill_payload(
            name=proposal.target_name,
            payload=payload,
            target_scope=proposal.target_scope,
            reject_secret_fields=True,
        )

    def _restore_target(self, proposal: SkillProposal) -> SkillTargetSnapshot:
        """Restore proposal base content; caller must hold the per-target lock."""
        path, canonical_path = self._target_path(
            target_scope=proposal.target_scope,
            target_name=proposal.target_name,
            project_id=str(proposal.project_id) if proposal.project_id else None,
        )
        if proposal.base_text is None:
            atomic_remove(path)
            return SkillTargetSnapshot(
                scope=proposal.target_scope,
                name=proposal.target_name,
                project_id=str(proposal.project_id) if proposal.project_id else None,
                path=path,
                canonical_path=canonical_path,
                exists=False,
                text=None,
                payload=None,
                sha256=None,
                version=None,
            )

        text = str(proposal.base_text)
        payload = self._validated_base_text_for_restore(
            proposal,
            path=path,
            text=text,
        )
        atomic_replace_text(path, text)
        sha256 = _digest_text(text)
        return SkillTargetSnapshot(
            scope=proposal.target_scope,
            name=proposal.target_name,
            project_id=str(proposal.project_id) if proposal.project_id else None,
            path=path,
            canonical_path=canonical_path,
            exists=True,
            text=text,
            payload=payload,
            sha256=sha256,
            version=f"sha256:{sha256}",
        )

    def _refresh_target_registry(
        self,
        *,
        target_scope: str,
        target_name: str,
        project_id: Optional[str],
    ) -> None:
        """Refresh registry only after the file/DB transition is durable."""
        path, _ = self._target_path(
            target_scope=target_scope,
            target_name=target_name,
            project_id=project_id,
        )
        if target_scope == "global":
            from ..skills.loader import load_skill_from_yaml
            from ..skills.registry import get_skill_registry, register_skill

            registry = get_skill_registry()
            registry.unregister(target_name)
            if path.is_file():
                skill = load_skill_from_yaml(path)
                if skill is None:
                    raise SkillLearningError(
                        "durable global Skill cannot be reloaded"
                    )
                register_skill(skill)
            return

        from ..skills.loader import load_project_skills

        if not project_id:
            raise SkillLearningError("project Skill lost project identity")
        load_project_skills(project_id)

    def _safe_refresh_target_registry(
        self,
        *,
        target_scope: str,
        target_name: str,
        project_id: Optional[str],
    ) -> None:
        try:
            self._refresh_target_registry(
                target_scope=target_scope,
                target_name=target_name,
                project_id=project_id,
            )
        except Exception:
            logger.exception(
                "Durable Skill registry refresh failed: %s/%s",
                target_scope,
                target_name,
            )

    def _compensate_locked(
        self,
        *,
        path: Path,
        expected_current_hash: Optional[str],
        restore_text: Optional[str],
        restore_hash: Optional[str],
    ) -> None:
        """Compensate only if the currently published hash is still ours."""
        observed = read_target_hash(path)
        if observed != expected_current_hash:
            raise SkillLearningConflict(
                "Skill target changed while compensating an incomplete transition"
            )
        if restore_text is None:
            atomic_remove(path)
        else:
            atomic_replace_text(path, restore_text)
        if read_target_hash(path) != restore_hash:
            raise SkillLearningError(
                "Skill transition compensation hash mismatch"
            )

    def _recover_transition_locked(
        self,
        *,
        proposal: SkillProposal,
        path: Path,
        requested_transition: str,
        journal: dict[str, Any],
    ) -> str:
        """Reconcile a crash journal using only DB state and before/after hashes."""
        if str(journal.get("proposal_id") or "") != str(proposal.id):
            raise SkillLearningConflict(
                "Skill target has an unfinished transition owned by another proposal"
            )
        transition = str(journal.get("transition") or "")
        if transition != requested_transition:
            raise SkillLearningConflict(
                f"Skill target has an unfinished {transition} transition"
            )

        observed = read_target_hash(path)
        target_project_id = (
            str(proposal.project_id) if proposal.project_id else None
        )

        if transition == "apply":
            before_hash = (
                proposal.base_hash if proposal.operation == "update" else None
            )
            after_hash = (
                journal.get("after_hash")
                or self._expected_proposal_hash(proposal)
            )

            if proposal.status == "applied" and proposal.rolled_back_at is None:
                expected = proposal.applied_hash or after_hash
                if expected is not None and observed == expected:
                    clear_skill_transition_journal(path)
                    self._safe_refresh_target_registry(
                        target_scope=proposal.target_scope,
                        target_name=proposal.target_name,
                        project_id=target_project_id,
                    )
                    return "applied"
                raise SkillLearningConflict(
                    "committed Skill apply journal does not match the live target"
                )

            if proposal.status != "pending":
                raise SkillLearningConflict(
                    "unfinished Skill apply journal conflicts with proposal state"
                )

            if observed == before_hash:
                clear_skill_transition_journal(path)
                return "ready"

            if after_hash is not None and observed == after_hash:
                restored = self._restore_target(proposal)
                if restored.sha256 != before_hash:
                    raise SkillLearningError(
                        "crash recovery could not restore the pre-apply Skill"
                    )
                clear_skill_transition_journal(path)
                self._safe_refresh_target_registry(
                    target_scope=proposal.target_scope,
                    target_name=proposal.target_name,
                    project_id=target_project_id,
                )
                return "ready"

            raise SkillLearningConflict(
                "unfinished Skill apply journal cannot be reconciled by hash"
            )

        before_hash = proposal.applied_hash
        after_hash = (
            proposal.base_hash if proposal.operation == "update" else None
        )
        if proposal.status != "applied":
            raise SkillLearningConflict(
                "unfinished Skill rollback journal conflicts with proposal state"
            )

        if proposal.rolled_back_at is not None:
            if observed == after_hash:
                clear_skill_transition_journal(path)
                self._safe_refresh_target_registry(
                    target_scope=proposal.target_scope,
                    target_name=proposal.target_name,
                    project_id=target_project_id,
                )
                return "rolled_back"
            raise SkillLearningConflict(
                "committed Skill rollback journal does not match the live target"
            )

        if observed == before_hash:
            clear_skill_transition_journal(path)
            return "ready"

        if observed == after_hash:
            expected_applied_hash = self._expected_proposal_hash(proposal)
            if expected_applied_hash != before_hash:
                raise SkillLearningConflict(
                    "applied Skill text cannot be reconstructed for rollback recovery"
                )
            written = self._write_target(proposal)
            if written.sha256 != before_hash:
                raise SkillLearningError(
                    "crash recovery could not restore the applied Skill"
                )
            clear_skill_transition_journal(path)
            self._safe_refresh_target_registry(
                target_scope=proposal.target_scope,
                target_name=proposal.target_name,
                project_id=target_project_id,
            )
            return "ready"

        raise SkillLearningConflict(
            "unfinished Skill rollback journal cannot be reconciled by hash"
        )

    async def _verify_commit_outcome(
        self,
        *,
        proposal_id: str,
        transition: str,
        after_hash: Optional[str],
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Resolve an ambiguous commit exception through a fresh DB session."""
        try:
            async with self._session() as verification_session:
                durable = await self._load_proposal(
                    verification_session,
                    proposal_id=proposal_id,
                    for_update=False,
                )
                _assert_proposal_content_private(durable)
                payload = durable.to_dict(include_history=True)
                if transition == "apply":
                    if (
                        durable.status == "applied"
                        and durable.rolled_back_at is None
                        and durable.applied_hash == after_hash
                    ):
                        return "after", payload
                    if durable.status == "pending":
                        return "before", payload
                elif transition == "rollback":
                    if (
                        durable.status == "applied"
                        and durable.rolled_back_at is not None
                    ):
                        return "after", payload
                    if (
                        durable.status == "applied"
                        and durable.rolled_back_at is None
                    ):
                        return "before", payload
        except Exception:
            logger.exception(
                "Unable to verify durable Skill %s commit outcome for %s",
                transition,
                proposal_id,
            )
            return "unknown", None
        return "unknown", None

    async def apply_proposal(
        self,
        proposal_id: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        async with self._session() as session:
            proposal = await self._load_proposal(
                session,
                proposal_id=proposal_id,
                for_update=True,
            )
            await self._authorize_proposal(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                write=True,
            )
            _assert_proposal_content_private(proposal)
            target_scope = str(proposal.target_scope)
            target_name = str(proposal.target_name)
            target_project_id = (
                str(proposal.project_id) if proposal.project_id else None
            )
            proposal_key = str(proposal.id)
            path, _ = self._target_path(
                target_scope=target_scope,
                target_name=target_name,
                project_id=target_project_id,
            )

            async with async_skill_target_lock(path):
                journal = read_skill_transition_journal(path)
                if journal is not None:
                    recovered = self._recover_transition_locked(
                        proposal=proposal,
                        path=path,
                        requested_transition="apply",
                        journal=journal,
                    )
                    if recovered == "applied":
                        return proposal.to_dict(include_history=True)

                if proposal.status == "applied" and proposal.rolled_back_at is None:
                    return proposal.to_dict(include_history=True)
                if proposal.status != "pending":
                    raise SkillLearningConflict("proposal is not pending")

                current = self.snapshot_target(
                    target_scope=target_scope,
                    target_name=target_name,
                    project_id=target_project_id,
                )
                create_conflict = (
                    proposal.operation == "create" and current.exists
                )
                update_conflict = proposal.operation == "update" and (
                    not current.exists or current.sha256 != proposal.base_hash
                )
                if create_conflict or update_conflict:
                    await self._mark_stale(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        observed_hash=current.sha256,
                        reason="base hash mismatch before apply",
                    )
                    await session.commit()
                    raise SkillLearningStale(
                        "Skill target changed; proposal is stale and no mutation "
                        "was applied",
                        proposal.to_dict(include_history=True),
                    )

                # Final locked CAS. Every C1 writer shares this target lock, so a
                # direct hash-B write that won the lock first is observed here
                # and can never be overwritten by this proposal.
                final_current = self.snapshot_target(
                    target_scope=target_scope,
                    target_name=target_name,
                    project_id=target_project_id,
                )
                final_create_conflict = (
                    proposal.operation == "create" and final_current.exists
                )
                final_update_conflict = proposal.operation == "update" and (
                    not final_current.exists
                    or final_current.sha256 != proposal.base_hash
                )
                if final_create_conflict or final_update_conflict:
                    await self._mark_stale(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        observed_hash=final_current.sha256,
                        reason="base hash mismatch at locked apply CAS",
                    )
                    await session.commit()
                    raise SkillLearningStale(
                        "Skill target changed at apply CAS; no mutation was applied",
                        proposal.to_dict(include_history=True),
                    )
                current = final_current

                expected_after_hash = self._expected_proposal_hash(proposal)
                write_skill_transition_journal(
                    path,
                    proposal_id=proposal_key,
                    transition="apply",
                    before_hash=current.sha256,
                    after_hash=expected_after_hash,
                )

                written: Optional[SkillTargetSnapshot] = None
                previous_status = proposal.status
                previous_applied_hash = proposal.applied_hash
                previous_applied_version = proposal.applied_version
                previous_applied_at = proposal.applied_at
                previous_updated_at = proposal.updated_at

                try:
                    written = self._write_target(proposal)
                    if (
                        expected_after_hash is not None
                        and written.sha256 != expected_after_hash
                    ):
                        raise SkillLearningError(
                            "Skill writer output does not match the journaled "
                            "apply hash"
                        )
                    write_skill_transition_journal(
                        path,
                        proposal_id=proposal_key,
                        transition="apply",
                        before_hash=current.sha256,
                        after_hash=written.sha256,
                    )
                    if not written.exists or not written.sha256:
                        raise SkillLearningError(
                            "Skill writer did not create a canonical target"
                        )
                    proposal.status = "applied"
                    proposal.applied_hash = written.sha256
                    proposal.applied_version = written.version
                    proposal.applied_at = datetime.utcnow()
                    proposal.updated_at = proposal.applied_at
                    await self._append_history(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        event="applied",
                        from_status=previous_status,
                        to_status="applied",
                        observed_hash=current.sha256,
                        result_hash=written.sha256,
                        details={"explicit_apply": True},
                    )
                except Exception:
                    await session.rollback()
                    if written is not None:
                        try:
                            self._compensate_locked(
                                path=path,
                                expected_current_hash=written.sha256,
                                restore_text=current.text,
                                restore_hash=current.sha256,
                            )
                            clear_skill_transition_journal(path)
                            self._safe_refresh_target_registry(
                                target_scope=target_scope,
                                target_name=target_name,
                                project_id=target_project_id,
                            )
                        except Exception:
                            logger.exception(
                                "Pre-commit Skill apply compensation failed; "
                                "journal retained: %s",
                                proposal_key,
                            )
                    elif read_target_hash(path) == current.sha256:
                        clear_skill_transition_journal(path)
                    proposal.status = previous_status
                    proposal.applied_hash = previous_applied_hash
                    proposal.applied_version = previous_applied_version
                    proposal.applied_at = previous_applied_at
                    proposal.updated_at = previous_updated_at
                    raise

                try:
                    await session.commit()
                except Exception as commit_error:
                    try:
                        await session.rollback()
                    except Exception:
                        logger.exception(
                            "Skill apply rollback after commit exception failed"
                        )

                    state, durable_payload = await self._verify_commit_outcome(
                        proposal_id=proposal_key,
                        transition="apply",
                        after_hash=written.sha256,
                    )
                    if (
                        state == "after"
                        and read_target_hash(path) == written.sha256
                    ):
                        clear_skill_transition_journal(path)
                        self._safe_refresh_target_registry(
                            target_scope=target_scope,
                            target_name=target_name,
                            project_id=target_project_id,
                        )
                        assert durable_payload is not None
                        return durable_payload

                    if state == "before":
                        try:
                            self._compensate_locked(
                                path=path,
                                expected_current_hash=written.sha256,
                                restore_text=current.text,
                                restore_hash=current.sha256,
                            )
                            clear_skill_transition_journal(path)
                            self._safe_refresh_target_registry(
                                target_scope=target_scope,
                                target_name=target_name,
                                project_id=target_project_id,
                            )
                        except Exception as recovery_error:
                            raise SkillLearningError(
                                "Skill apply commit failed and compensation could "
                                "not be verified; transition journal retained"
                            ) from recovery_error
                        raise commit_error

                    raise SkillLearningError(
                        "Skill apply commit outcome is unknown; transition journal "
                        "retained"
                    ) from commit_error

                try:
                    clear_skill_transition_journal(path)
                except Exception as exc:
                    raise SkillLearningError(
                        "Skill apply committed but journal cleanup is pending"
                    ) from exc

                if read_target_hash(path) == written.sha256:
                    self._safe_refresh_target_registry(
                        target_scope=target_scope,
                        target_name=target_name,
                        project_id=target_project_id,
                    )

                await session.refresh(proposal)
                return proposal.to_dict(include_history=True)

    async def reject_proposal(
        self,
        proposal_id: str,
        *,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        async with self._session() as session:
            proposal = await self._load_proposal(
                session,
                proposal_id=proposal_id,
                for_update=True,
            )
            await self._authorize_proposal(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                write=True,
            )
            # Rejection must remain available for a legacy unsafe row. Its
            # serializer is privacy-redacted by the model, and reject never
            # publishes Skill content.
            if proposal.status == "rejected":
                return proposal.to_dict(include_history=True)
            if proposal.status != "pending":
                raise SkillLearningConflict("only pending proposals can be rejected")
            proposal.status = "rejected"
            proposal.rejected_at = datetime.utcnow()
            proposal.updated_at = proposal.rejected_at
            await self._append_history(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                event="rejected",
                from_status="pending",
                to_status="rejected",
                details={"reason": _bounded_string(reason, 1_000)},
            )
            await session.commit()
            await session.refresh(proposal)
            return proposal.to_dict(include_history=True)

    async def rollback_proposal(
        self,
        proposal_id: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        async with self._session() as session:
            proposal = await self._load_proposal(
                session,
                proposal_id=proposal_id,
                for_update=True,
            )
            await self._authorize_proposal(
                session,
                proposal=proposal,
                actor_id=str(actor_id),
                write=True,
            )
            # Rollback publishes the prior canonical file, not proposed_content.
            # Permit rollback of an unsafe proposed version so it can be removed,
            # but never restore an unsafe plaintext base snapshot.
            _assert_proposal_content_private(
                proposal,
                include_proposed=False,
                include_base=True,
            )
            target_scope = str(proposal.target_scope)
            target_name = str(proposal.target_name)
            target_project_id = (
                str(proposal.project_id) if proposal.project_id else None
            )
            proposal_key = str(proposal.id)
            path, _ = self._target_path(
                target_scope=target_scope,
                target_name=target_name,
                project_id=target_project_id,
            )

            async with async_skill_target_lock(path):
                journal = read_skill_transition_journal(path)
                if journal is not None:
                    recovered = self._recover_transition_locked(
                        proposal=proposal,
                        path=path,
                        requested_transition="rollback",
                        journal=journal,
                    )
                    if recovered == "rolled_back":
                        return proposal.to_dict(include_history=True)

                if proposal.status != "applied":
                    raise SkillLearningConflict(
                        "only applied proposals can be rolled back"
                    )
                if proposal.rolled_back_at is not None:
                    return proposal.to_dict(include_history=True)

                current = self.snapshot_target(
                    target_scope=target_scope,
                    target_name=target_name,
                    project_id=target_project_id,
                )
                if not current.exists or current.sha256 != proposal.applied_hash:
                    await self._mark_stale(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        observed_hash=current.sha256,
                        reason="applied Skill changed before rollback",
                    )
                    await session.commit()
                    raise SkillLearningStale(
                        "applied Skill changed; rollback performed no mutation",
                        proposal.to_dict(include_history=True),
                    )

                final_current = self.snapshot_target(
                    target_scope=target_scope,
                    target_name=target_name,
                    project_id=target_project_id,
                )
                if (
                    not final_current.exists
                    or final_current.sha256 != proposal.applied_hash
                ):
                    await self._mark_stale(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        observed_hash=final_current.sha256,
                        reason="applied hash mismatch at locked rollback CAS",
                    )
                    await session.commit()
                    raise SkillLearningStale(
                        "applied Skill changed at rollback CAS; no mutation was "
                        "applied",
                        proposal.to_dict(include_history=True),
                    )
                current = final_current

                expected_restored_hash = (
                    proposal.base_hash
                    if proposal.operation == "update"
                    else None
                )
                write_skill_transition_journal(
                    path,
                    proposal_id=proposal_key,
                    transition="rollback",
                    before_hash=current.sha256,
                    after_hash=expected_restored_hash,
                )

                restored: Optional[SkillTargetSnapshot] = None
                previous_rolled_back_at = proposal.rolled_back_at
                previous_updated_at = proposal.updated_at
                try:
                    restored = self._restore_target(proposal)
                    if proposal.operation == "create":
                        if restored.exists:
                            raise SkillLearningError(
                                "create rollback did not remove Skill"
                            )
                        result_hash = None
                    else:
                        if (
                            not restored.exists
                            or restored.sha256 != expected_restored_hash
                        ):
                            raise SkillLearningError(
                                "update rollback did not restore base Skill"
                            )
                        result_hash = restored.sha256

                    proposal.rolled_back_at = datetime.utcnow()
                    proposal.updated_at = proposal.rolled_back_at
                    await self._append_history(
                        session,
                        proposal=proposal,
                        actor_id=str(actor_id),
                        event="rollback",
                        from_status="applied",
                        to_status="applied",
                        observed_hash=current.sha256,
                        result_hash=result_hash,
                        details={
                            "restored_base_hash": expected_restored_hash
                        },
                    )
                except Exception:
                    await session.rollback()
                    if restored is not None:
                        try:
                            self._compensate_locked(
                                path=path,
                                expected_current_hash=restored.sha256,
                                restore_text=current.text,
                                restore_hash=current.sha256,
                            )
                            clear_skill_transition_journal(path)
                            self._safe_refresh_target_registry(
                                target_scope=target_scope,
                                target_name=target_name,
                                project_id=target_project_id,
                            )
                        except Exception:
                            logger.exception(
                                "Pre-commit Skill rollback compensation failed; "
                                "journal retained: %s",
                                proposal_key,
                            )
                    elif read_target_hash(path) == current.sha256:
                        clear_skill_transition_journal(path)
                    proposal.rolled_back_at = previous_rolled_back_at
                    proposal.updated_at = previous_updated_at
                    raise

                try:
                    await session.commit()
                except Exception as commit_error:
                    try:
                        await session.rollback()
                    except Exception:
                        logger.exception(
                            "Skill rollback DB rollback after commit exception failed"
                        )

                    state, durable_payload = await self._verify_commit_outcome(
                        proposal_id=proposal_key,
                        transition="rollback",
                        after_hash=expected_restored_hash,
                    )
                    if (
                        state == "after"
                        and read_target_hash(path) == expected_restored_hash
                    ):
                        clear_skill_transition_journal(path)
                        self._safe_refresh_target_registry(
                            target_scope=target_scope,
                            target_name=target_name,
                            project_id=target_project_id,
                        )
                        assert durable_payload is not None
                        return durable_payload

                    if state == "before":
                        try:
                            self._compensate_locked(
                                path=path,
                                expected_current_hash=expected_restored_hash,
                                restore_text=current.text,
                                restore_hash=current.sha256,
                            )
                            clear_skill_transition_journal(path)
                            self._safe_refresh_target_registry(
                                target_scope=target_scope,
                                target_name=target_name,
                                project_id=target_project_id,
                            )
                        except Exception as recovery_error:
                            raise SkillLearningError(
                                "Skill rollback commit failed and compensation "
                                "could not be verified; transition journal retained"
                            ) from recovery_error
                        raise commit_error

                    raise SkillLearningError(
                        "Skill rollback commit outcome is unknown; transition "
                        "journal retained"
                    ) from commit_error

                try:
                    clear_skill_transition_journal(path)
                except Exception as exc:
                    raise SkillLearningError(
                        "Skill rollback committed but journal cleanup is pending"
                    ) from exc

                if read_target_hash(path) == expected_restored_hash:
                    self._safe_refresh_target_registry(
                        target_scope=target_scope,
                        target_name=target_name,
                        project_id=target_project_id,
                    )

                await session.refresh(proposal)
                return proposal.to_dict(include_history=True)
