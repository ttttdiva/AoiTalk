"""Authenticated portfolio definition, owner-bound setup and readback."""
from __future__ import annotations

from typing import Annotated, Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from .media_operations_routes import PersonaDetailResponse, _actor, _invoke, _principal_projection, _with_session
from .media_operations_setup_routes import PlatformAccountDetailResponse
from ..services.media_brand_portfolio_service import MediaBrandPortfolioService, portfolio_definition


class _PortfolioModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrandSourceResponse(_PortfolioModel):
    library_file_id: str
    file_id: str
    filename: str
    sha256: str
    size_bytes: int
    total_lines: int
    access: str
    byte_verification: str
    zip_library_file_id: str
    zip_sha256: str | None


class BrandChannelDefinition(_PortfolioModel):
    platform: str
    source: Literal["v6", "libfile_b6232d2776d481919e7ed52dff698c97"]
    role: str


class BrandDefinitionResponse(_PortfolioModel):
    brand_key: str
    project_name: str
    display_name: str
    channels: list[BrandChannelDefinition]
    channels_exhaustive: Literal[False]
    target: str
    content_pillars: list[str]
    qa: list[str]
    kpis: list[str]
    references: list[str]
    reference_urls: list[str]
    unknowns: list[str]
    source_paths: list[str]
    documents: dict[str, str]
    document_sha256: dict[str, str]
    workflow: list[str]


class BrandHeadquartersDefinition(_PortfolioModel):
    project_name: str
    public_persona: bool
    documents: dict[str, str]


class BrandPortfolioDefinitionResponse(_PortfolioModel):
    schema_name: Literal["mediaops-brand-portfolio/v1"] = Field(alias="schema")
    source: BrandSourceResponse
    headquarters: BrandHeadquartersDefinition
    brands: list[BrandDefinitionResponse] = Field(min_length=8, max_length=8)


class BrandChannelReadback(BrandChannelDefinition):
    platform_account_id: str | None
    binding_status: Literal["unconfigured", "unsupported_platform", "service_selection_required"]
    account: PlatformAccountDetailResponse | None


class BrandReadbackResponse(_PortfolioModel):
    definition: BrandDefinitionResponse
    project_id: str | None
    persona: PersonaDetailResponse | None
    channels: list[BrandChannelReadback]
    editorial_program_id: str | None
    content_item_id: str | None


class BrandPortfolioResponse(_PortfolioModel):
    schema_name: Literal["mediaops-brand-portfolio/v1"] = Field(alias="schema")
    source: BrandSourceResponse
    owner_user_id: str
    status: Literal["ready", "not_configured"]
    headquarters_project_id: str | None
    headquarters_public_persona: Literal[False]
    brands: list[BrandReadbackResponse] = Field(min_length=8, max_length=8)


class BrandPortfolioSetupRequest(_PortfolioModel):
    expected_owner_user_id: UUID
    expected_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Optional explicit adoption of inspected owner Projects. Missing entries
    # use an unambiguous exact-name Project or create through ProjectRepository.
    project_bindings: dict[str, UUID] = Field(default_factory=dict, max_length=9)


def create_media_brand_portfolio_router(get_db_manager: Callable[[], Any],
                                       get_user_from_request: Callable[..., Any],
                                       require_auth_dependency: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix="/brand-portfolio", tags=["operations-media"])
    service = MediaBrandPortfolioService()

    async def current_actor(request: Request) -> dict[str, Any]:
        return _principal_projection(await _actor(get_user_from_request, request))

    @router.get("/definition", response_model=BrandPortfolioDefinitionResponse,
                operation_id="media_get_brand_portfolio_definition")
    async def definition(request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        await current_actor(request)
        return portfolio_definition()

    @router.get("", response_model=BrandPortfolioResponse, operation_id="media_get_brand_portfolio")
    async def readback(request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(service.readback, session, actor))

    @router.post("/setup", response_model=BrandPortfolioResponse, operation_id="media_setup_brand_portfolio")
    async def setup(payload: BrandPortfolioSetupRequest, request: Request,
                    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
                    _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(
            service.setup, session, actor, **payload.model_dump(), idempotency_key=idempotency_key))

    return router
