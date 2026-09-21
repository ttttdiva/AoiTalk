"""Application composition for employee management and the shared work lanes.

Factories accept explicit server-owned dependencies so a disposable QA runtime
can exercise the real routes/coordinator with deterministic provider adapters.
There is no environment or request switch that installs a test adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..features import Features


@dataclass
class AiEmployeeServices:
    registry: Any
    credential_vault: Any
    action_policy: Any
    action_execution: Any
    automation: Any
    telephony: Any = None


def build_ai_employee_services(
    db_manager: Any,
    *,
    config: Any = None,
    registry: Any = None,
    credential_vault: Any = None,
    invoker: Any = None,
    telephony: Any = None,
) -> AiEmployeeServices:
    from .agent_action_policy_service import AgentActionPolicyService
    from .agent_automation_service import AgentAutomationService
    from .external_action_execution_service import ExternalActionExecutionService
    from .integration_action_registry import IntegrationActionRegistry
    from .integration_credential_vault_service import IntegrationCredentialVaultService
    from .telephony_service import TelephonyService, TelephonyTransferAdapter

    vault = credential_vault if credential_vault is not None else IntegrationCredentialVaultService(config=config)
    phone = telephony if telephony is not None else TelephonyService(db_manager, config=config, vault=vault)
    registry = registry if registry is not None else IntegrationActionRegistry(
        telephony_adapter_factory=lambda: TelephonyTransferAdapter(phone),
    )
    policy = AgentActionPolicyService(db_manager, registry=registry, credential_vault=vault, config=config)
    execution = ExternalActionExecutionService(policy_service=policy)
    phone.action_policy_service = policy
    phone.action_execution_service = execution
    return AiEmployeeServices(
        registry=registry,
        credential_vault=vault,
        action_policy=policy,
        action_execution=execution,
        automation=AgentAutomationService(db_manager, config=config, registry=registry, invoker=invoker),
        telephony=phone,
    )


def register_ai_employee_work(coordinator: Any, services: AiEmployeeServices) -> None:
    """Register ordinary employee work on the application's sole coordinator."""
    if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
        return
    from .agent_automation_runtime import AutomationRuleExecutionAdapter, AutomationRuleWorkSource
    from .external_action_work_source import ExternalActionWorkSource
    from .external_action_execution_service import ExternalActionExecutionAdapter

    db_manager, config = coordinator._db_manager, coordinator.config
    coordinator.register_source(AutomationRuleWorkSource(db_manager, config=config))
    coordinator.register_adapter(AutomationRuleExecutionAdapter(
        db_manager, config=config, action_policy_service=services.action_policy,
        invoker=services.automation.invoker,
    ))
    coordinator.register_source(ExternalActionWorkSource(policy_service=services.action_policy))
    coordinator.register_adapter(ExternalActionExecutionAdapter(execution_service=services.action_execution))
