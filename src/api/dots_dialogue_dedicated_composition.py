"""Composition for the one approved AoiTalk DOTS stdio/Tunnel connection."""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from .routes.dots_dialogue_chat_routes import create_dots_dialogue_chat_router
from .routes.dots_dialogue_routes import create_dots_dialogue_router, dots_dialogue_route_enabled
from ..services.dots_dialogue_dispatcher import (
    DEFAULT_DISPATCH_BATCH,
    MAX_DISPATCH_BATCH,
    DotsDialogueDispatcherLifecycle,
    DotsDialogueOutboxDispatcher,
    SqlAlchemyDotsDialogueOutboxBackend,
)
from ..services.dots_dialogue_http_bridge import create_dots_dialogue_http_bridge_router
from ..services.dots_dialogue_service import DedicatedDotsPrincipal, DotsDialogueService

try:
    from plugins.AoiTalk_Plugin.aoitalk_plugin.dots_dialogue_dedicated import (
        DedicatedDotsConfigurationError,
        DedicatedDotsConnectionConfig,
        load_dedicated_dots_connection_config,
    )
except Exception:  # pragma: no cover - optional Plugin package unavailable
    DedicatedDotsConfigurationError = ValueError
    DedicatedDotsConnectionConfig = None
    load_dedicated_dots_connection_config = None


def _api_principal(connection: Any) -> DedicatedDotsPrincipal:
    return DedicatedDotsPrincipal(
        app_id=connection.app_id,
        plugin_id=connection.plugin_id,
        tunnel_id=connection.tunnel_id,
        runtime_alias=connection.runtime_alias,
        profile_alias=connection.profile_alias,
        owner_user_id=UUID(connection.owner_user_id),
        conversation_id=UUID(connection.conversation_id),
    )


def _bearer_capability(request: Request) -> str | None:
    headers = request.scope.get("headers", ())
    matches = [value for name, value in headers
               if isinstance(name, bytes) and name.lower() == b"authorization"]
    if len(matches) != 1 or not isinstance(matches[0], bytes):
        return None
    try:
        value = matches[0].decode("latin-1")
    except UnicodeDecodeError:
        return None
    scheme, separator, credential = value.partition(" ")
    if (scheme.lower() != "bearer" or not separator or not credential
            or credential != credential.strip() or any(char.isspace() for char in credential)):
        return None
    return credential


def _config_enabled(config: Any, *, enterprise_profile: bool,
                    profile_allows_external: bool | None) -> bool:
    if (not isinstance(config, Mapping) or enterprise_profile is not False
            or profile_allows_external is not True):
        return False
    try:
        return dots_dialogue_route_enabled(config, enterprise_profile=False)
    except Exception:
        return False


def _callback_sender(supplied: Any | None, *, enabled: bool) -> Any | None:
    if supplied is not None:
        return supplied if callable(getattr(supplied, "post", None)) else None
    if not enabled:
        return None
    try:
        from plugins.AoiTalk_Plugin.aoitalk_plugin.dots_events_transport import SecureHttpsCallbackSender

        return SecureHttpsCallbackSender()
    except Exception:
        return None


def create_dots_dialogue_dedicated_composition(
    *,
    get_db_manager: Callable[[], Any] | None,
    get_user_from_request: Callable[[Request], Any] | None,
    require_auth_dependency: Callable[..., Any] | None,
    config: Mapping[str, Any] | None,
    enterprise_profile: bool,
    profile_allows_external: bool | None,
    connection_config: Any | None = None,
    callback_sender: Any | None = None,
    dispatcher_enabled: bool = False,
    dispatcher_max_deliveries: int = DEFAULT_DISPATCH_BATCH,
    dispatcher_interval_seconds: float = 1.0,
) -> APIRouter:
    """Build owner/chat/bridge routes and the existing bounded dispatcher.

    Identity comes only from the exact, validated local Plugin .env capability
    for bridge calls and the server-owned fixed config for owner operations.
    The normal AoiTalk owner dependency remains mandatory for owner-side calls.
    """
    feature_enabled = _config_enabled(
        config, enterprise_profile=enterprise_profile,
        profile_allows_external=profile_allows_external,
    )
    if connection_config is None and callable(load_dedicated_dots_connection_config):
        try:
            connection_config = load_dedicated_dots_connection_config()
        except DedicatedDotsConfigurationError:
            connection_config = None
    config_valid = (DedicatedDotsConnectionConfig is not None
                    and isinstance(connection_config, DedicatedDotsConnectionConfig))
    api_principal = None
    if config_valid:
        try:
            candidate = _api_principal(connection_config)
            if (candidate.fingerprint == connection_config.principal.fingerprint
                    and str(candidate.conversation_id) == connection_config.conversation_id):
                api_principal = candidate
            else:
                config_valid = False
        except Exception:
            config_valid = False
    enabled = feature_enabled and config_valid

    def owner_principal(_request: Request) -> DedicatedDotsPrincipal | None:
        return api_principal if enabled else None

    def bridge_principal(request: Request) -> DedicatedDotsPrincipal | None:
        if not enabled or connection_config is None:
            return None
        credential = _bearer_capability(request)
        if not connection_config.accepts_capability(credential):
            return None
        return api_principal

    db_provider = get_db_manager if callable(get_db_manager) else _missing_db
    owner_provider = get_user_from_request if callable(get_user_from_request) else _missing_owner
    auth_dependency = require_auth_dependency if callable(require_auth_dependency) else _missing_auth
    callback_transport = _callback_sender(callback_sender, enabled=enabled)
    bridge_enabled = enabled and callback_transport is not None

    owner_router = create_dots_dialogue_router(
        get_db_manager=db_provider,
        get_user_from_request=owner_provider,
        require_auth_dependency=auth_dependency,
        get_mcp_principal=owner_principal,
        enabled=enabled,
        profile_allows_external=enabled,
    )
    bridge_router = create_dots_dialogue_http_bridge_router(
        get_db_manager=db_provider,
        principal_provider=bridge_principal,
        callback_sender=callback_transport,
        enabled=bridge_enabled,
        profile_allows_external=bridge_enabled,
    )
    chat_router = create_dots_dialogue_chat_router(
        get_db_manager=db_provider,
        get_user_from_request=owner_provider,
        require_auth_dependency=auth_dependency,
        principal=api_principal,
        enabled=enabled,
    )

    dispatcher = None
    dispatcher_requested = (
        dispatcher_enabled is True and enabled and bridge_enabled
        and api_principal is not None and callable(get_db_manager)
        and isinstance(dispatcher_max_deliveries, int)
        and not isinstance(dispatcher_max_deliveries, bool)
        and 1 <= dispatcher_max_deliveries <= MAX_DISPATCH_BATCH
        and isinstance(dispatcher_interval_seconds, (int, float))
        and not isinstance(dispatcher_interval_seconds, bool)
        and dispatcher_interval_seconds > 0
    )
    if dispatcher_requested:
        async def session_provider() -> Any:
            manager = get_db_manager()
            if inspect.isawaitable(manager):
                manager = await manager
            session = manager.get_session()
            return await session if inspect.isawaitable(session) else session

        try:
            backend = SqlAlchemyDotsDialogueOutboxBackend(
                session_provider=session_provider,
                service=DotsDialogueService(enabled=True),
                principal_fingerprint=api_principal.fingerprint,
                dedicated_conversation_id=api_principal.conversation_id,
            )
            dispatcher = DotsDialogueOutboxDispatcher(
                backend=backend,
                callback_sender=callback_transport,
                enabled=True,
            )
        except Exception:
            dispatcher = None

    lifecycle = DotsDialogueDispatcherLifecycle(
        dispatcher,
        enabled=dispatcher_requested and dispatcher is not None,
        max_deliveries=dispatcher_max_deliveries,
        interval_seconds=dispatcher_interval_seconds,
    )

    router = APIRouter()
    router.include_router(owner_router)
    router.include_router(bridge_router)
    router.include_router(chat_router)
    router.dots_dialogue_dedicated_enabled = enabled  # type: ignore[attr-defined]
    router.dots_dialogue_bridge_enabled = bridge_enabled  # type: ignore[attr-defined]
    router.dots_dialogue_principal = api_principal  # type: ignore[attr-defined]
    router.dots_dialogue_callback_sender = callback_transport  # type: ignore[attr-defined]
    router.dots_dialogue_dispatcher = dispatcher  # type: ignore[attr-defined]
    router.dots_dialogue_dispatcher_lifecycle = lifecycle  # type: ignore[attr-defined]
    router.dots_dialogue_dispatcher_enabled = lifecycle.enabled  # type: ignore[attr-defined]
    return router


async def _missing_owner(_request: Request) -> None:
    raise HTTPException(status_code=503, detail={"code": "owner_auth_unavailable"})


async def _missing_auth() -> None:
    raise HTTPException(status_code=503, detail={"code": "owner_auth_unavailable"})


def _missing_db() -> None:
    raise HTTPException(status_code=503, detail={"code": "dots_dialogue_database_unavailable"})


__all__ = ["create_dots_dialogue_dedicated_composition"]
