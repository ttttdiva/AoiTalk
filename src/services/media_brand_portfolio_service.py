"""Owner-bound v6 portfolio setup using the existing MediaOps entities.

No remote accounts, provider calls, tasks, publishing or paid generation. The
source is a design hypothesis, not new benchmark evidence or a verified result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.models import MEDIA_PLATFORM_VALUES, Persona, PersonaIntakeSlot, Project, ProjectMember, User
from ..memory.project_repository import ProjectRepository
from .media_operations_service import (
    MediaOperationsAuthorizationError, MediaOperationsConflictError,
    MediaOperationsService, MediaOperationsValidationError,
    _actor_id, _as_uuid, _idempotency_key, sha256_json,
)
from .media_operations_setup_service import MediaOperationsSetupService
from .media_operations_research_service import MediaOperationsResearchService

SOURCE_SHA256 = "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd"
SCHEMA = "mediaops-brand-portfolio/v1"
META = "media_brand_portfolio"
ROOT = Path(__file__).resolve().parents[2] / "resources" / "mediaops"


def portfolio_definition() -> dict[str, Any]:
    raw = (ROOT / "sns_portfolio_v6.md").read_bytes()
    data = json.loads((ROOT / "sns_portfolio_v6.json").read_text(encoding="utf-8"))
    if (hashlib.sha256(raw).hexdigest() != SOURCE_SHA256
            or data["source"]["sha256"] != SOURCE_SHA256
            or data["source"]["size_bytes"] != len(raw)):
        raise MediaOperationsConflictError("v6 original source checksum mismatch")
    for brand in data["brands"]:
        for name, document in brand["documents"].items():
            if hashlib.sha256(document.encode()).hexdigest() != brand["document_sha256"][name]:
                raise MediaOperationsConflictError("v6 Persona document checksum mismatch")
            if document not in raw.decode("utf-8"):
                raise MediaOperationsConflictError("v6 Persona document differs from the original")
    return data


class _DeferredCommit:
    """Compose existing mutation services into one transaction."""

    async def _commit(self, session: Any) -> None:
        # Each existing _flush_commit still flushes and validates its entity.
        # Only the portfolio command commits the complete graph.
        pass


class _Personas(_DeferredCommit, MediaOperationsService):
    pass


class _Accounts(_DeferredCommit, MediaOperationsSetupService):
    pass


class _Editorial(_DeferredCommit, MediaOperationsResearchService):
    pass


class MediaBrandPortfolioService:
    async def _projects(self, session: Any, owner: UUID) -> list[Project]:
        return list((await session.scalars(select(Project).where(
            Project.owner_id == owner, Project.deleted_at.is_(None)
        ))).all())

    async def readback(self, session: Any, actor: Any) -> dict[str, Any]:
        owner = _actor_id(actor)
        definition = portfolio_definition()
        projects = await self._projects(session, owner)
        marked = [p for p in projects if (p.project_metadata or {}).get(META)]
        by_key: dict[str, Project] = {}
        for project in marked:
            meta = project.project_metadata[META]
            if meta.get("schema") != SCHEMA or meta.get("source_sha256") != SOURCE_SHA256:
                raise MediaOperationsConflictError("portfolio source changed; explicit migration required")
            key = meta.get("brand_key")
            if key in by_key:
                raise MediaOperationsConflictError("duplicate brand Project mapping")
            by_key[key] = project
        headquarters = by_key.get("headquarters")
        if headquarters is not None:
            await self._assert_private_headquarters(session, headquarters, owner)
        result = []
        for brand in definition["brands"]:
            project = by_key.get(brand["brand_key"])
            entry = {"definition": brand, "project_id": None, "persona": None,
                     "channels": [], "editorial_program_id": None, "content_item_id": None}
            if project is not None:
                meta = project.project_metadata[META]
                if (project.is_completed or project.name != brand["project_name"]
                        or headquarters is None
                        or meta.get("headquarters_project_id") != str(headquarters.id)
                        or meta.get("definition_hash") != sha256_json(brand)):
                    raise MediaOperationsConflictError("brand Project identity or headquarters changed")
                persona = await MediaOperationsService().get_persona(session, actor, meta["persona_id"])
                revision = persona["current_revision"]
                if (persona["owner_user_id"] != str(owner) or persona["project_id"] != str(project.id)
                        or persona["id"] != meta["persona_id"]
                        or revision["research_policy"].get("source_sha256") != SOURCE_SHA256):
                    raise MediaOperationsConflictError("brand Persona scope/source changed")
                persona_rows = list((await session.scalars(select(Persona).join(
                    PersonaIntakeSlot, PersonaIntakeSlot.persona_id == Persona.id).where(
                    Persona.project_id == project.id
                ))).all())
                if len(persona_rows) != 1:
                    raise MediaOperationsConflictError("one public Persona must occupy one Project")
                channels = []
                for channel in meta["channels"]:
                    actual = None
                    if channel["platform_account_id"]:
                        actual = await MediaOperationsSetupService().get_platform_account(
                            session, actor, channel["platform_account_id"])
                        if (actual["owner_user_id"] != str(owner)
                                or actual["project_id"] != str(project.id)
                                or actual["persona_id"] != persona["id"]
                                or actual["platform"] != channel["platform"]):
                            raise MediaOperationsConflictError("brand channel owner/Persona/Project changed")
                    channels.append({**channel, "account": actual})
                editorial = MediaOperationsResearchService()
                program = await editorial.get_editorial_program(session, actor, meta["editorial_program_id"])
                item = await editorial.get_content_item(session, actor, meta["content_item_id"])
                if (program["persona_id"] != persona["id"] or program["project_id"] != str(project.id)
                        or item["editorial_program_id"] != program["id"]
                        or item["persona_revision_id"] != meta["persona_revision_id"]
                        or item["project_id"] != str(project.id)):
                    raise MediaOperationsConflictError("portfolio editorial graph changed")
                entry.update(project_id=str(project.id), persona=persona, channels=channels,
                             editorial_program_id=program["id"], content_item_id=item["id"])
            result.append(entry)
        ready = headquarters is not None and all(b["persona"] for b in result)
        return {"schema": SCHEMA, "source": definition["source"], "owner_user_id": str(owner),
                "status": "ready" if ready else "not_configured",
                "headquarters_project_id": str(headquarters.id) if headquarters else None,
                "headquarters_public_persona": False, "brands": result}

    async def _assert_private_headquarters(self, session: Any, project: Project, owner: UUID) -> None:
        if (project.owner_id != owner or project.name != "00_SNS事業本部"
                or project.is_completed or project.deleted_at is not None):
            raise MediaOperationsAuthorizationError("headquarters must be the owner's active private Project")
        others = await session.scalar(select(ProjectMember.id).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id != owner).limit(1))
        persona = await session.scalar(select(Persona.id).where(Persona.project_id == project.id).limit(1))
        if others is not None or persona is not None or project.allow_join_requests:
            raise MediaOperationsConflictError("SNS headquarters must be private and have no public Persona")

    async def setup(self, session: Any, actor: Any, *, expected_owner_user_id: Any,
                    expected_source_sha256: str, project_bindings: dict[str, Any] | None = None,
                    idempotency_key: str) -> dict[str, Any]:
        owner = _actor_id(actor)
        if owner != _as_uuid(expected_owner_user_id, "expected_owner_user_id"):
            raise MediaOperationsAuthorizationError("authenticated owner differs from the expected owner")
        _idempotency_key(idempotency_key)
        if expected_source_sha256 != SOURCE_SHA256:
            raise MediaOperationsConflictError("v6 expected source checksum mismatch")
        definition = portfolio_definition()
        # Serialize setup by authenticated owner, including distinct request keys.
        user = await session.scalar(select(User).where(User.id == owner).with_for_update())
        if user is None:
            raise MediaOperationsAuthorizationError("authenticated owner is unavailable")
        bindings = {k: _as_uuid(v, "project_bindings") for k, v in (project_bindings or {}).items()}
        specs = [{"brand_key": "headquarters", **definition["headquarters"]}, *definition["brands"]]
        keys = {b["brand_key"] for b in specs}
        if set(bindings) - keys or len(set(bindings.values())) != len(bindings):
            raise MediaOperationsValidationError("each brand requires a distinct known Project mapping")
        existing = await self._projects(session, owner)
        selected: dict[str, Project | None] = {}
        for spec in specs:
            key = spec["brand_key"]
            if key in bindings:
                project = await session.get(Project, bindings[key])
                if project is None or project.owner_id != owner:
                    raise MediaOperationsAuthorizationError("Project mapping crosses the authenticated owner")
                matches = [project]
            else:
                matches = [p for p in existing if p.name == spec["project_name"]]
            if len(matches) > 1:
                raise MediaOperationsConflictError("ambiguous existing Project; supply an explicit mapping")
            project = matches[0] if matches else None
            if project is not None:
                if (project.name != spec["project_name"] or project.is_completed or project.deleted_at is not None):
                    raise MediaOperationsConflictError("Project name/lifecycle differs from v6")
                meta = (project.project_metadata or {}).get(META)
                if meta and (meta.get("source_sha256") != SOURCE_SHA256 or meta.get("brand_key") != key):
                    raise MediaOperationsConflictError("existing Project has a different brand/source mapping")
                if key == "headquarters":
                    await self._assert_private_headquarters(session, project, owner)
                elif not meta:
                    persona = await session.scalar(select(Persona.id).join(
                        PersonaIntakeSlot, PersonaIntakeSlot.persona_id == Persona.id
                    ).where(Persona.project_id == project.id).limit(1))
                    if persona is not None:
                        raise MediaOperationsConflictError("existing Persona requires an explicit source migration; no duplicate created")
            selected[key] = project
        selected_ids = [p.id for p in selected.values() if p is not None]
        if len(set(selected_ids)) != len(selected_ids):
            raise MediaOperationsValidationError("one Project cannot contain multiple public brands")
        # A complete replay is read-only and never reverts later editing or activates anything.
        snapshot = await self.readback(session, actor)
        if snapshot["status"] == "ready":
            actual_bindings = {"headquarters": snapshot["headquarters_project_id"], **{
                b["definition"]["brand_key"]: b["project_id"] for b in snapshot["brands"]}}
            if any(str(value) != actual_bindings[key] for key, value in bindings.items()):
                raise MediaOperationsConflictError("requested Project mapping differs from the existing portfolio")
            return snapshot
        if any((p.project_metadata or {}).get(META) for p in selected.values() if p is not None):
            raise MediaOperationsConflictError("incomplete portfolio mapping requires repair; no duplicate created")
        try:
            for spec in specs:
                key = spec["brand_key"]
                if selected[key] is None:
                    project = await ProjectRepository.create_project(
                        session, owner_id=owner, name=spec["project_name"],
                        description=spec["documents"]["project_brief"],
                        slug=f"sns-{owner.hex}-{key}", allow_join_requests=False, commit=False)
                    from .project_information_docs import ensure_project_information_doc
                    await ensure_project_information_doc(session, project=project, user_id=owner)
                    selected[key] = project
            headquarters = selected["headquarters"]
            assert headquarters is not None
            headquarters.project_metadata = {**(headquarters.project_metadata or {}), META: {
                "schema": SCHEMA, "brand_key": "headquarters", "source_sha256": SOURCE_SHA256,
                "public_persona": False, "source": definition["source"],
                "custom_instructions": definition["headquarters"]["documents"]["custom_instructions"]}}
            media, accounts, editorial = _Personas(), _Accounts(), _Editorial()
            for brand in definition["brands"]:
                project = selected[brand["brand_key"]]
                assert project is not None
                prefix = f"sns-v6:{brand['brand_key']}:{SOURCE_SHA256[:12]}"
                policy = {"schema": SCHEMA, "source_sha256": SOURCE_SHA256,
                          "library_file_id": definition["source"]["library_file_id"],
                          "brand_slug": brand["brand_key"],
                          "custom_instructions_sha256": brand["document_sha256"]["custom_instructions"],
                          "project_brief_sha256": brand["document_sha256"]["project_brief"],
                          "source_paths": brand["source_paths"], "references": brand["references"],
                          "desired_channels": [c["role"] for c in brand["channels"]],
                          "channels_exhaustive": False,
                          "qa": brand["qa"], "target": brand["target"], "unknowns": brand["unknowns"]}
                persona = await media.create_persona(
                    session, actor, project_id=project.id, intake_slot=1,
                    display_name=brand["display_name"], summary=brand["documents"]["project_brief"],
                    creative_direction=brand["documents"]["custom_instructions"],
                    platforms=[c["platform"] for c in brand["channels"] if c["platform"] in MEDIA_PLATFORM_VALUES],
                    content_pillars=brand["content_pillars"], kpi_objectives=brand["kpis"],
                    research_policy=policy, default_language="ja", locale="ja-JP", timezone="Asia/Tokyo",
                    parent_brand_ref=f"sns-v6:{brand['brand_key']}", state="draft", idempotency_key=prefix+":persona")
                revision_id = persona["current_revision"]["id"]
                for name, source_hash in {"v6": SOURCE_SHA256, **brand["document_sha256"]}.items():
                    await media.attach_persona_resource(session, actor, persona["id"],
                        resource_kind="supporting_document", label=f"v6:{brand['brand_key']}:{name}",
                        provenance={"type": "artifact", "sha256": source_hash, "mime_type": "text/markdown"},
                        idempotency_key=prefix+":source:"+name)
                channels = []
                for channel in brand["channels"]:
                    platform = channel["platform"]
                    account = None
                    if platform in MEDIA_PLATFORM_VALUES:
                        account = await accounts.create_platform_account(
                            session, actor, project_id=project.id, persona_id=persona["id"],
                            platform=platform, account_ref=f"sns-v6:{brand['brand_key']}:{platform}",
                            display_name=brand["display_name"], status="paused", account_type="profile",
                            locale="ja-JP", timezone="Asia/Tokyo", idempotency_key=prefix+":channel:"+platform)
                    channels.append({**channel, "platform_account_id": account["id"] if account else None,
                                     "binding_status": "unconfigured" if account else (
                                         "service_selection_required" if platform in {"membership", "download_sales"}
                                         else "unsupported_platform")})
                routine = await editorial.create_research_routine(
                    session, actor, persona_id=persona["id"], name=brand["display_name"]+" 原典・制作仮説",
                    objective=brand["target"], questions=brand["qa"],
                    cadence="manual", enabled=False, state="draft", idempotency_key=prefix+":routine")
                run = await editorial.start_research_run(session, actor, research_routine_id=routine["id"],
                    routine_version=1, omissions=["v6設計原文のみ。制作QA・公開・売上検証は未実施。"],
                    idempotency_key=prefix+":source-run")
                finding = await editorial.append_research_finding(session, actor, run["id"], kind="hypothesis",
                    statement="正式v6に基づく制作仮説: "+brand["target"],
                    evidence=[{"type": "artifact", "sha256": SOURCE_SHA256, "mime_type": "text/markdown",
                               "label": definition["source"]["library_file_id"], "note": "採用済み設計原文。成功実測・素材利用権の証明ではない。"}],
                    idempotency_key=prefix+":finding")
                program = await editorial.create_editorial_program(
                    session, actor, persona_id=persona["id"], name=brand["display_name"]+" 制作計画",
                    objective=brand["target"], content_type="music" if brand["brand_key"]=="un_real" else "media",
                    target_platforms=persona["current_revision"]["platforms"],
                    target_account_refs=[f"sns-v6:{brand['brand_key']}:{c['platform']}" for c in channels if c["platform_account_id"]],
                    required_resources=brand["source_paths"], draft_generation_policy="human_review",
                    state="draft", enabled=False, cadence="manual", idempotency_key=prefix+":program")
                brief = "\n".join([brand["documents"]["project_brief"], "制作QA（未実施）:",
                                    *brand["qa"], "未確定事項:", *brand["unknowns"]])
                item = await editorial.create_content_item(session, actor, editorial_program_id=program["id"],
                    persona_revision_id=revision_id, title=brand["display_name"]+" 初期制作設計",
                    brief=brief, finding_ids=[finding["id"]], objective=brand["target"],
                    content_type="music" if brand["brand_key"]=="un_real" else "media",
                    source_refs=[definition["source"]["library_file_id"]],
                    desired_assets=brand["content_pillars"], idempotency_key=prefix+":item")
                project.project_metadata = {**(project.project_metadata or {}), META: {
                    "schema": SCHEMA, "brand_key": brand["brand_key"], "source_sha256": SOURCE_SHA256,
                    "public_persona": True, "headquarters_project_id": str(headquarters.id),
                    "persona_id": persona["id"], "persona_revision_id": revision_id, "channels": channels,
                    "editorial_program_id": program["id"], "content_item_id": item["id"],
                    "definition_hash": sha256_json(brand)}}
            await session.flush()
            result = await self.readback(session, actor)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
