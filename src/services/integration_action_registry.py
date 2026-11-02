"""Code-owned action definitions; persisted strings cannot install adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from . import procurement_action
from .marketplace import crowdworks_action
from .agent_identity_service import sha256_json


class ActionPolicyError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = self.message = code
        self.status_code = status_code


@dataclass(frozen=True)
class IntegrationActionResult:
    status: str
    provider_attempt_ref: str | None = None
    provider_receipt_ref: str | None = None
    remote_resource_id: str | None = None
    remote_url: str | None = None
    remote_status: str | None = None
    observed_at: datetime | None = None
    safe_error_code: str | None = None


class DefinitelyNotSent(Exception):
    """Adapter guarantees the order was never submitted."""


class IntegrationActionAdapter(Protocol):
    adapter_key: str
    adapter_version: str

    async def prepare(self, *, payload, connection, credential): ...
    async def execute(self, *, action_id, attempt_id, execution_key, payload, connection, credential, quote): ...


def _specialized(*_args):
    raise ActionPolicyError("specialized_action_required")


@dataclass(frozen=True)
class IntegrationActionDefinition:
    action_type: str
    display_name: str
    category: str
    schema_version: str
    required_capabilities: tuple[str, ...]
    connection_provider_keys: tuple[str, ...]
    adapter_key: str | None
    adapter_version: str | None
    status: str
    normalize_policy: Callable
    normalize_payload: Callable
    payload_fields: tuple[str, ...] = ()
    adapter_factory: Callable | None = None
    execution_lane: str = "specialized"
    credential_mode: str = "vault"

    @property
    def extraction_schema(self):
        return {key: {"type": "integer" if key == "quantity" else "string"} for key in self.payload_fields}

    def dedupe_key(self, payload: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
        # Quantity and policy id deliberately excluded: another report/policy
        # cannot turn the same replenishment target into a second order.
        if self.action_type == "engagement.submit_application":
            return sha256_json({"connection_id": str(binding["connection_id"]),
                                "action_type": self.action_type,
                                "opportunity_id": payload["opportunity_id"]})
        return sha256_json({"connection_id": str(binding["connection_id"]),
                            "action_type": self.action_type,
                            "item_ref": payload["item_ref"], "ship_to_ref": payload["ship_to_ref"]})


class IntegrationActionRegistry:
    def __init__(self, *, procurement_adapter_factory=None, engagement_adapter_factory=None):
        adapter = procurement_adapter_factory() if procurement_adapter_factory else None
        self._definitions = {
            key: IntegrationActionDefinition(key, key, "legacy", "1", (), (), None, None,
                                             "manual", _specialized, _specialized)
            for key in ("engagement.submit_application", "media.publish_content", "media.update_content",
                        "media.delete_content", "media.release_product")
        }
        engagement = engagement_adapter_factory() if engagement_adapter_factory else None
        self._definitions["engagement.submit_application"] = IntegrationActionDefinition(
            "engagement.submit_application", "Submit CrowdWorks application", "engagement", "1",
            ("external_action_propose",), ("crowdworks",),
            engagement.adapter_key if engagement else "crowdworks_browser",
            engagement.adapter_version if engagement else "1",
            "automatable" if engagement else "unavailable",
            crowdworks_action.normalize_policy, crowdworks_action.normalize_payload,
            ("opportunity_id", "application_draft_id", "source_url", "message", "offered_price", "currency", "delivery_estimate"),
            engagement_adapter_factory, "durable", "browser_session")
        self._definitions["procurement.place_order"] = IntegrationActionDefinition(
            "procurement.place_order", "Place procurement order", "procurement", "1",
            ("external_action_propose",), ("procurement",),
            adapter.adapter_key if adapter else "procurement", adapter.adapter_version if adapter else "1",
            "automatable" if adapter else "unavailable", procurement_action.normalize_policy,
            procurement_action.normalize_payload, ("item_ref", "quantity", "ship_to_ref", "currency"),
            procurement_adapter_factory, "durable")

    def get(self, action_type):
        return self._definitions.get(action_type)

    def definitions(self):
        return tuple(self._definitions.values())

    def require(self, action_type):
        definition = self.get(action_type)
        if definition is None:
            raise ActionPolicyError("action_type_unregistered")
        return definition

    def require_executable(self, action_type):
        definition = self.require(action_type)
        if definition.status != "automatable" or definition.adapter_factory is None:
            raise ActionPolicyError("provider_unavailable", 409)
        return definition

    def safe_catalog(self):
        return [{"action_type": d.action_type, "display_name": d.display_name, "category": d.category,
                 "schema_version": d.schema_version, "status": d.status,
                 "required_capabilities": list(d.required_capabilities),
                 "connection_provider_keys": list(d.connection_provider_keys),
                 "adapter_key": d.adapter_key, "adapter_version": d.adapter_version,
                 "execution_lane": d.execution_lane,
                 "payload_fields": list(d.payload_fields), "extraction_schema": d.extraction_schema,
                 "constraints_schema": (procurement_action.ProcurementPolicy.model_json_schema() if d.category == "procurement"
                                        else crowdworks_action.CrowdWorksPolicy.model_json_schema() if d.category == "engagement" else None),
                 "credential_mode": d.credential_mode,
                 "execution_owner": "specialized" if d.category == "legacy" else "external_action"}
                for d in self._definitions.values()]
