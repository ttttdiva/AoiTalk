"""Code-owned action definitions; persisted strings cannot install adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from . import procurement_action
from .agent_identity_service import sha256_json
from uuid import UUID


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


def _transfer_policy(value):
    import re
    if not isinstance(value, dict) or set(value) != {"route_id", "allowed_destination_keys"}:
        raise ActionPolicyError("action_policy_constraint_failed")
    route = str(UUID(str(value["route_id"])))
    keys = value["allowed_destination_keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 64 or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", k) for k in keys):
        raise ActionPolicyError("action_policy_constraint_failed")
    return {"route_id": route, "allowed_destination_keys": sorted(set(keys))}


def _transfer_payload(value, policy):
    if not isinstance(value, dict) or set(value) != {"telephony_call_id", "destination_key"}:
        raise ActionPolicyError("action_policy_constraint_failed")
    if value["destination_key"] not in _transfer_policy(policy)["allowed_destination_keys"]:
        raise ActionPolicyError("action_policy_constraint_failed")
    return {"telephony_call_id": str(UUID(str(value["telephony_call_id"]))), "destination_key": value["destination_key"]}


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

    @property
    def extraction_schema(self):
        return {key: {"type": "integer" if key == "quantity" else "string"} for key in self.payload_fields}

    def dedupe_key(self, payload: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
        # Quantity and policy id deliberately excluded: another report/policy
        # cannot turn the same replenishment target into a second order.
        if self.action_type == "telephony.transfer_call":
            return sha256_json({"connection_id": str(binding["connection_id"]), "call": payload["telephony_call_id"]})
        return sha256_json({"connection_id": str(binding["connection_id"]),
                            "action_type": self.action_type,
                            "item_ref": payload["item_ref"], "ship_to_ref": payload["ship_to_ref"]})


class IntegrationActionRegistry:
    def __init__(self, *, procurement_adapter_factory=None, telephony_adapter_factory=None):
        adapter = procurement_adapter_factory() if procurement_adapter_factory else None
        self._definitions = {
            key: IntegrationActionDefinition(key, key, "legacy", "1", (), (), None, None,
                                             "manual", _specialized, _specialized)
            for key in ("engagement.submit_application", "media.publish_content", "media.update_content",
                        "media.delete_content", "media.release_product")
        }
        self._definitions["procurement.place_order"] = IntegrationActionDefinition(
            "procurement.place_order", "Place procurement order", "procurement", "1",
            ("external_action_propose",), ("procurement",),
            adapter.adapter_key if adapter else "procurement", adapter.adapter_version if adapter else "1",
            "automatable" if adapter else "unavailable", procurement_action.normalize_policy,
            procurement_action.normalize_payload, ("item_ref", "quantity", "ship_to_ref", "currency"),
            procurement_adapter_factory, "durable")
        phone = telephony_adapter_factory() if telephony_adapter_factory else None
        self._definitions["telephony.transfer_call"] = IntegrationActionDefinition(
            "telephony.transfer_call", "Transfer active call", "telephony", "1", ("telephony_control",),
            ("openai_realtime_sip",), phone.adapter_key if phone else "openai_realtime_sip",
            phone.adapter_version if phone else "1", "automatable" if phone else "unavailable",
            _transfer_policy, _transfer_payload, ("telephony_call_id", "destination_key"), telephony_adapter_factory, "realtime")

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
                 "constraints_schema": procurement_action.ProcurementPolicy.model_json_schema() if d.category == "procurement" else None,
                 "execution_owner": "specialized" if d.category == "legacy" else "external_action"}
                for d in self._definitions.values()]
