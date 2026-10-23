"""Authenticated owner-only career source and accepted-profile routes."""

from __future__ import annotations

import inspect
import re
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...memory.models.workspace_files import WorkspaceFileEntry
from ...services.personal_career_service import (
    CareerConflictError,
    CareerError,
    CareerNotFoundError,
    CareerPermissionError,
    CareerValidationError,
    PersonalCareerService,
    owner_actor,
)
from ...services.search_sources import SearchScope, SearchSourceResolver, SourceRef, SourceUnavailable
from .personal_ledger_routes import require_interactive_owner

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MIME_PATTERN = r"^[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,64}$"


class CareerSourceRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN, strict=True)
    mime_type: str = Field(pattern=_MIME_PATTERN, max_length=128, strict=True)
    byte_size: int = Field(ge=1, le=10_485_760, strict=True)
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class CareerSourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: bool
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class CareerProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: dict[str, list[str]]
    source_refs: list[UUID] = Field(max_length=16)
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class CareerProfileConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN, strict=True)
    owner_confirmed: Literal[True]
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class CareerContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted_profile_ref: UUID


class CareerSourceOut(BaseModel):
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    mime_type: str
    byte_size: int
    selected: bool | None = None


class CareerSourcePage(BaseModel):
    sources: list[CareerSourceOut]


class CareerAvailableSourceOut(BaseModel):
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    mime_type: str
    byte_size: int
    registered: bool
    selected: bool


class CareerAvailableSourcePage(BaseModel):
    sources: list[CareerAvailableSourceOut]


class CareerProfileFieldsOut(BaseModel):
    skills: list[str]
    preferences: list[str]
    constraints: list[str]


class CareerProfileVersionOut(BaseModel):
    version_ref: UUID
    version: int
    confirmed: bool
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN)
    profile: CareerProfileFieldsOut
    source_refs: list[UUID]
    unconfirmed_claim_count: int
    actor_kind: Literal["user_action", "model"]
    recorded_at: str | None


class CareerProfileVersionPage(BaseModel):
    versions: list[CareerProfileVersionOut]


class CareerProfileConfirmationOut(BaseModel):
    version_ref: UUID
    version: int
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN)


class CareerContextOut(BaseModel):
    profile: CareerProfileFieldsOut
    source_refs: list[UUID]
    needs_review: list[str]


def _career_http_error(exc: CareerError) -> HTTPException:
    if isinstance(exc, CareerNotFoundError):
        return HTTPException(404, "career_resource_not_found")
    if isinstance(exc, CareerConflictError):
        return HTTPException(409, "career_conflict")
    if isinstance(exc, CareerPermissionError):
        return HTTPException(403, "owner_action_required")
    if isinstance(exc, CareerValidationError):
        return HTTPException(422, "career_validation")
    return HTTPException(400, "career_operation_failed")


def _source_state(view: Any) -> dict[str, Any]:
    return {**view.source.to_contract(), "selected": bool(view.selected)}


def _profile_version(view: Any) -> dict[str, Any]:
    return {
        "version_ref": str(view.version_ref),
        "version": view.version,
        "confirmed": view.confirmed,
        "confirmed_digest": view.confirmed_digest,
        "profile": view.profile.to_contract(),
        "source_refs": [str(ref) for ref in view.source_refs],
        "unconfirmed_claim_count": view.unconfirmed_claim_count,
        "actor_kind": view.actor_kind,
        "recorded_at": view.recorded_at.isoformat() if view.recorded_at else None,
    }


def register_personal_career_routes(
    app: FastAPI,
    server,
    *,
    service: PersonalCareerService | None = None,
    resolver_factory=None,
) -> None:
    session_factory = lambda: server._db_manager.get_session()
    career = service or PersonalCareerService.from_session_factory(session_factory)

    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"

    async def owner(request: Request, response: Response) -> str:
        no_store(response)
        owner_id = await require_interactive_owner(server, request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "same_origin_json_required")
        return owner_id

    async def guard(request: Request, owner_id: str) -> None:
        current = await require_interactive_owner(server, request)
        if current != owner_id:
            raise HTTPException(403, "owner_scope_changed")

    def current_guard(request: Request, owner_id: str):
        async def check():
            await guard(request, owner_id)

        return check

    async def open_resolver(owner_id: str):
        if resolver_factory is not None:
            resolver = resolver_factory(UUID(owner_id))
            if inspect.isawaitable(resolver):
                resolver = await resolver
            return None, resolver
        session = server._db_manager.get_session()
        if inspect.isawaitable(session):
            session = await session
        resolver = SearchSourceResolver(
            session,
            SearchScope(
                user_id=UUID(owner_id), project_id=None,
                project_scope_restricted=True,
            ),
        )
        return session, resolver

    async def close_session(session) -> None:
        if session is not None:
            result = session.close()
            if inspect.isawaitable(result):
                await result

    async def verify_source(owner_id: str, body: CareerSourceRegister, resolver, request: Request) -> None:
        try:
            resolved = await resolver.resolve(
                SourceRef("workspace_file", str(body.source_ref)), open_file=False
            )
        except SourceUnavailable:
            raise HTTPException(404, "career_source_unavailable") from None
        await guard(request, owner_id)
        metadata = resolved.metadata if isinstance(resolved.metadata, dict) else {}
        if (
            metadata.get("files_scope") != "user"
            or metadata.get("files_owner_id") != owner_id
            or metadata.get("content_hash") != body.source_sha256
            or metadata.get("mime_type") != body.mime_type
        ):
            raise HTTPException(409, "career_source_changed")
        # Resolver metadata intentionally omits file size. Query only the owner-scoped
        # index header and compare the client descriptor before registration.
        result = await resolver.session.execute(
            select(WorkspaceFileEntry.size_bytes).where(
                WorkspaceFileEntry.id == body.source_ref,
                WorkspaceFileEntry.scope == "user",
                WorkspaceFileEntry.owner_id == UUID(owner_id),
                WorkspaceFileEntry.status == "indexable",
                WorkspaceFileEntry.deleted_at.is_(None),
            )
        )
        await guard(request, owner_id)
        size = result.scalar_one_or_none()
        if size is None or int(size) != body.byte_size:
            raise HTTPException(409, "career_source_changed")

    @app.get("/api/personal-career/sources", response_model=CareerSourcePage)
    async def list_sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        owner_id: str = Depends(owner),
    ):
        try:
            rows = await career.list_sources(owner_actor(owner_id), limit=limit)
            await guard(request, owner_id)
            return {"sources": [_source_state(view) for view in rows]}
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.get("/api/personal-career/sources/available", response_model=CareerAvailableSourcePage)
    async def list_available_sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        owner_id: str = Depends(owner),
    ):
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            result = await session.execute(
                select(WorkspaceFileEntry)
                .where(
                    WorkspaceFileEntry.scope == "user",
                    WorkspaceFileEntry.owner_id == UUID(owner_id),
                    WorkspaceFileEntry.status == "indexable",
                    WorkspaceFileEntry.deleted_at.is_(None),
                    WorkspaceFileEntry.content_hash.is_not(None),
                    WorkspaceFileEntry.mime.is_not(None),
                    WorkspaceFileEntry.size_bytes > 0,
                    WorkspaceFileEntry.size_bytes <= 10_485_760,
                )
                .order_by(WorkspaceFileEntry.last_seen_at.desc(), WorkspaceFileEntry.id.desc())
                .limit(limit)
            )
            await guard(request, owner_id)
            rows = result.scalars().all()
            registered_rows = await career.list_sources(owner_actor(owner_id), limit=200)
            await guard(request, owner_id)
            registered = {view.source.source_ref: view for view in registered_rows}
            sources = []
            for row in rows:
                try:
                    resolved = await resolver.resolve(SourceRef("workspace_file", str(row.id)), open_file=False)
                except SourceUnavailable:
                    await guard(request, owner_id)
                    continue
                await guard(request, owner_id)
                metadata = resolved.metadata if isinstance(resolved.metadata, dict) else {}
                mime_type = metadata.get("mime_type")
                if (
                    metadata.get("files_scope") != "user"
                    or metadata.get("files_owner_id") != owner_id
                    or metadata.get("content_hash") != row.content_hash
                    or not isinstance(mime_type, str)
                    or not re.fullmatch(_MIME_PATTERN, mime_type)
                ):
                    continue
                state = registered.get(row.id)
                sources.append(CareerAvailableSourceOut(
                    source_ref=row.id,
                    source_sha256=row.content_hash,
                    mime_type=mime_type,
                    byte_size=int(row.size_bytes),
                    registered=state is not None,
                    selected=bool(state.selected) if state is not None else False,
                ))
            return CareerAvailableSourcePage(sources=sources)
        except CareerError as exc:
            raise _career_http_error(exc) from None
        finally:
            await close_session(session)

    @app.post("/api/personal-career/sources", response_model=CareerSourceOut)
    async def register_source(
        body: CareerSourceRegister,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            await verify_source(owner_id, body, resolver, request)
            view = await career.register_source(
                owner_actor(owner_id),
                {
                    "source_ref": body.source_ref,
                    "source_sha256": body.source_sha256,
                    "mime_type": body.mime_type,
                    "byte_size": body.byte_size,
                },
                body.idempotency_key,
            )
            await guard(request, owner_id)
            return view.to_contract()
        except CareerError as exc:
            raise _career_http_error(exc) from None
        finally:
            await close_session(session)

    @app.post("/api/personal-career/sources/{source_ref}/selection", response_model=CareerSourceOut)
    async def set_source_selection(
        source_ref: UUID,
        body: CareerSourceSelection,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        try:
            view = await career.set_source_selected(
                owner_actor(owner_id), source_ref, body.selected, body.idempotency_key
            )
            await guard(request, owner_id)
            return {**view.source.to_contract(), "selected": view.selected}
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.get("/api/personal-career/profile-versions", response_model=CareerProfileVersionPage)
    async def list_profile_versions(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        owner_id: str = Depends(owner),
    ):
        try:
            rows = await career.list_profile_versions(owner_actor(owner_id), limit=limit)
            await guard(request, owner_id)
            return {"versions": [_profile_version(view) for view in rows]}
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.post("/api/personal-career/profile-versions", response_model=CareerProfileVersionOut)
    async def create_profile_version(
        body: CareerProfileCreate,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        try:
            view = await career.create_profile_version(
                owner_actor(owner_id),
                profile=body.profile,
                source_refs=body.source_refs,
                unconfirmed_claims=(),
                idempotency_key=body.idempotency_key,
            )
            await guard(request, owner_id)
            return _profile_version(view)
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.get("/api/personal-career/profile-versions/{version_ref}", response_model=CareerProfileVersionOut)
    async def get_profile_version(
        version_ref: UUID,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        try:
            view = await career.get_profile_version(
                owner_actor(owner_id), version_ref,
                guard=current_guard(request, owner_id),
            )
            await guard(request, owner_id)
            return _profile_version(view)
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.post("/api/personal-career/profile-versions/{version_ref}/confirm", response_model=CareerProfileConfirmationOut)
    async def confirm_profile_version(
        version_ref: UUID,
        body: CareerProfileConfirmation,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        try:
            view = await career.confirm_profile_version(
                owner_actor(owner_id), version_ref=version_ref,
                confirmed_digest=body.confirmed_digest,
                idempotency_key=body.idempotency_key,
                owner_confirmed=body.owner_confirmed,
            )
            await guard(request, owner_id)
            return view.to_contract()
        except CareerError as exc:
            raise _career_http_error(exc) from None

    @app.post("/api/personal-career/context", response_model=CareerContextOut)
    async def accepted_context(
        body: CareerContextRequest,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        session = None
        try:
            actor = owner_actor(owner_id)
            version = await career.get_profile_version(
                actor, body.accepted_profile_ref,
                guard=current_guard(request, owner_id),
            )
            await guard(request, owner_id)
            if not version.confirmed:
                raise HTTPException(409, "profile_version_not_confirmed")
            selected = await career.list_selected_sources(actor, limit=64)
            await guard(request, owner_id)
            selected_by_ref = {source.source_ref: source for source in selected}
            if any(ref not in selected_by_ref for ref in version.source_refs):
                raise HTTPException(409, "profile_source_selection_changed")

            session, resolver = await open_resolver(owner_id)
            for source_ref in version.source_refs:
                registered = selected_by_ref[source_ref]
                try:
                    resolved = await resolver.resolve(
                        SourceRef("workspace_file", str(source_ref)), open_file=False
                    )
                except SourceUnavailable:
                    raise HTTPException(409, "profile_source_changed") from None
                await guard(request, owner_id)
                metadata = resolved.metadata if isinstance(resolved.metadata, dict) else {}
                if (
                    metadata.get("files_scope") != "user"
                    or metadata.get("files_owner_id") != owner_id
                    or metadata.get("content_hash") != registered.source_sha256
                ):
                    raise HTTPException(409, "profile_source_changed")

            context = await career.build_context(
                actor, accepted_profile_ref=body.accepted_profile_ref,
                guard=current_guard(request, owner_id),
            )
            await guard(request, owner_id)
            # Re-read selection after context construction so a concurrent scope
            # change cannot turn an accepted version into a partial context.
            selected_after = await career.list_selected_sources(actor, limit=64)
            await guard(request, owner_id)
            selected_refs_after = {source.source_ref for source in selected_after}
            if any(ref not in selected_refs_after for ref in version.source_refs):
                raise HTTPException(409, "profile_source_selection_changed")
            return context.to_contract()
        except CareerError as exc:
            raise _career_http_error(exc) from None
        finally:
            await close_session(session)
