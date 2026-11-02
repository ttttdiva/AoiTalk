"""Application composition for employee management and the shared work lanes.

Factories accept explicit server-owned dependencies so a disposable QA runtime
can exercise the real routes/coordinator with deterministic provider adapters.
There is no environment or request switch that installs a test adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from ..features import Features


_CROWDWORKS_AGENT_SCAN_SHADOW_KEY = "crowdworks.agent_scan_shadow_enabled"
_CROWDWORKS_AGENT_SCAN_POLICY_KEY = "crowdworks.agent_scan_policy_id"


def _crowdworks_setting(config: Any, key: str) -> Any:
    """Read a dotted application setting from flat or nested config objects."""

    missing = object()
    value: Any = missing
    if isinstance(config, Mapping):
        value = config.get(key, missing)
        if value is missing:
            value = config
            for component in key.split("."):
                if not isinstance(value, Mapping) or component not in value:
                    value = missing
                    break
                value = value[component]
    else:
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                value = getter(key, missing)
            except TypeError:
                try:
                    value = getter(key)
                except Exception:
                    value = missing
            except Exception:
                value = missing
            if value is missing:
                parts = key.split(".")
                try:
                    value = getter(parts[0], missing)
                    for component in parts[1:]:
                        if not isinstance(value, Mapping) or component not in value:
                            value = missing
                            break
                        value = value[component]
                except Exception:
                    value = missing
    return value


def _crowdworks_agent_scan_shadow_settings(config: Any) -> tuple[bool, UUID | None]:
    """Require both the explicit shadow switch and a parseable dedicated Policy ID."""

    shadow_enabled = _crowdworks_setting(config, _CROWDWORKS_AGENT_SCAN_SHADOW_KEY) is True
    raw_policy_id = _crowdworks_setting(config, _CROWDWORKS_AGENT_SCAN_POLICY_KEY)
    try:
        policy_id = raw_policy_id if isinstance(raw_policy_id, UUID) else UUID(str(raw_policy_id))
    except (ValueError, TypeError, AttributeError):
        policy_id = None
    if not shadow_enabled or policy_id is None:
        return False, None
    return True, policy_id


@dataclass
class AiEmployeeServices:
    registry: Any
    credential_vault: Any
    action_policy: Any
    action_execution: Any
    automation: Any


def build_ai_employee_services(
    db_manager: Any,
    *,
    config: Any = None,
    registry: Any = None,
    credential_vault: Any = None,
    invoker: Any = None,
) -> AiEmployeeServices:
    from .agent_action_policy_service import AgentActionPolicyService
    from .agent_automation_service import AgentAutomationService
    from .external_action_execution_service import ExternalActionExecutionService
    from .integration_action_registry import IntegrationActionRegistry
    from .integration_credential_vault_service import IntegrationCredentialVaultService
    vault = credential_vault if credential_vault is not None else IntegrationCredentialVaultService()
    if registry is None:
        from .marketplace.crowdworks_browser_adapter import CrowdWorksBrowserAdapter
        registry = IntegrationActionRegistry(
            engagement_adapter_factory=lambda: CrowdWorksBrowserAdapter(config),
        )
    policy = AgentActionPolicyService(db_manager, registry=registry, credential_vault=vault, config=config)
    execution = ExternalActionExecutionService(policy_service=policy)
    return AiEmployeeServices(
        registry=registry,
        credential_vault=vault,
        action_policy=policy,
        action_execution=execution,
        automation=AgentAutomationService(db_manager, config=config, registry=registry, invoker=invoker),
    )


def _register_crowdworks_contract_monitor(
    coordinator: Any,
    services: AiEmployeeServices,
    *,
    config: Any,
    agent_scan_source: Any,
) -> None:
    from .marketplace.crowdworks_contract_monitor_runtime import (
        CrowdWorksContractMonitorExecutionAdapter,
        CrowdWorksContractMonitorWorkSource,
    )

    source = CrowdWorksContractMonitorWorkSource(
        policy_service=services.action_policy, agent_scan_source=agent_scan_source,
    )
    coordinator.register_source(source)
    coordinator.register_adapter(CrowdWorksContractMonitorExecutionAdapter(source=source, config=config))


def _register_crowdworks_agent_scan(
    coordinator: Any,
    *,
    config: Any,
    agent_scan_source: Any,
) -> None:
    from .marketplace.crowdworks_agent_scan import CrowdWorksAgentScanExecutionAdapter

    coordinator.register_source(agent_scan_source)
    coordinator.register_adapter(CrowdWorksAgentScanExecutionAdapter(config=config))


def _register_crowdworks_fulfillment(
    coordinator: Any,
    *,
    config: Any,
) -> None:
    from .marketplace.crowdworks_fulfillment import (
        CrowdWorksFulfillmentWorkSource,
        fulfillment_adapters,
    )

    coordinator.register_source(CrowdWorksFulfillmentWorkSource())
    for adapter in fulfillment_adapters(config):
        coordinator.register_adapter(adapter)


def register_ai_employee_work(
    coordinator: Any,
    services: AiEmployeeServices,
) -> None:
    """Register ordinary employee work on the application's sole coordinator."""
    if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
        return
    from .agent_automation_runtime import AutomationRuleExecutionAdapter, AutomationRuleWorkSource
    from .external_action_work_source import ExternalActionWorkSource
    from .external_action_execution_service import ExternalActionExecutionAdapter

    from .agent_delegation_runtime import register_agent_delegation_work

    db_manager, config = coordinator._db_manager, coordinator.config
    register_agent_delegation_work(coordinator)
    coordinator.register_source(AutomationRuleWorkSource(db_manager, config=config))
    coordinator.register_adapter(AutomationRuleExecutionAdapter(
        db_manager, config=config, action_policy_service=services.action_policy,
        invoker=services.automation.invoker,
    ))
    coordinator.register_source(ExternalActionWorkSource(policy_service=services.action_policy))
    coordinator.register_adapter(ExternalActionExecutionAdapter(execution_service=services.action_execution))
    from .marketplace.crowdworks_agent_scan import CrowdWorksAgentScanWorkSource

    shadow_enabled, agent_scan_policy_id = _crowdworks_agent_scan_shadow_settings(config)
    agent_scan_source = CrowdWorksAgentScanWorkSource(
        policy_service=services.action_policy,
        enabled=shadow_enabled,
        agent_scan_policy_id=agent_scan_policy_id,
    )
    _register_crowdworks_contract_monitor(
        coordinator, services, config=config, agent_scan_source=agent_scan_source
    )
    _register_crowdworks_agent_scan(
        coordinator, config=config, agent_scan_source=agent_scan_source
    )
    _register_crowdworks_fulfillment(coordinator, config=config)
