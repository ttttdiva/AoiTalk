"""Compose dedicated management APIs with shared application-owned services."""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ..features import Features


def _employee_runtime_enabled():
    # Evaluate before any database dependency so a disabled deployment exposes
    # a consistent 404 even when employee storage is unavailable.
    if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
        raise HTTPException(status_code=404, detail="employee_runtime_disabled")


def register_ai_employee_routes(server, require_auth):
    from ..services.ai_employee_platform import build_ai_employee_services
    from .agent_action_policy_routes import create_agent_action_policy_router
    from .agent_automation_routes import create_agent_automation_router
    from .integration_action_routes import create_integration_action_router
    from .integration_credential_routes import create_integration_credential_router
    from .telephony_routes import create_telephony_router

    services = build_ai_employee_services(server._db_manager, config=server.config)
    server.ai_employee_services = services
    deps = dict(get_db_manager=lambda: server._db_manager,
                get_user_from_request=server._get_user_info_from_request,
                require_auth_dependency=require_auth)
    routers = [
        create_agent_action_policy_router(**deps, policy_service=services.action_policy, config=server.config),
        create_integration_action_router(**deps, registry=services.registry),
        create_integration_credential_router(**deps, vault=services.credential_vault),
        create_telephony_router(**deps, service=services.telephony, config=server.config),
        create_agent_automation_router(
            require_auth, server._get_user_info_from_request,
            service=services.automation, db_manager=server._db_manager, config=server.config,
        ),
    ]
    for router in routers:
        server.app.include_router(router, dependencies=[Depends(_employee_runtime_enabled)])
    shutdown_hooks = getattr(server, "_shutdown_background_tasks", None)
    if isinstance(shutdown_hooks, list):
        shutdown_hooks.append(services.telephony.close)
    return services
