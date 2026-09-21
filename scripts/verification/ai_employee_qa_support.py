"""Explicit fixture providers for the isolated employee QA launcher only."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import json
from dataclasses import replace
import hmac
from pathlib import Path
import importlib.util
import sys
import unicodedata
from uuid import UUID, uuid4

from scripts.verification.ai_employee_qa import private_state, write_json


class SemanticFixture:
    """Deterministic provider response, not evidence of real model accuracy."""

    async def evaluate(self, *, source_text, **_kwargs):
        from src.services.agent_automation_invoker import AutomationDecision

        text = unicodedata.normalize("NFKC", source_text).strip()
        matched = text in {
            "水なくなりました", "ウォーターサーバーの水が空です", "交換用の水がもうありません",
            "水切れです。補充お願いします", "ウォーターサーバーの水がなくなりました", "水切れです",
        }
        return AutomationDecision(matched=matched, reason_code="matched" if matched else "not_matched", extracted={}, usage={})


def fixture_dependencies(manifest: dict, *, evidence_name="provider-evidence.json"):
    from src.memory.database import get_database_manager
    from src.services.integration_action_registry import IntegrationActionRegistry
    from src.services.integration_credential_vault_service import (
        IntegrationCredentialVaultService, IntegrationCredentialProviderVerifier, IntegrationCredentialVerifierPolicy,
    )
    from scripts.verification.ai_employee_qa import REPO

    # The repo tests directory is not a Python package; an installed third-party
    # `tests` package can otherwise shadow this explicitly assigned adapter.
    spec = importlib.util.spec_from_file_location("_employee_qa_provider_tests", REPO / "tests/test_integration_action_execution.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    DeterministicProcurementTestAdapter = module.DeterministicProcurementTestAdapter

    private = private_state(manifest)
    key = base64.b64decode(private["field_key"], validate=True)

    class Keys:
        def active_key_id(self):
            return "employee-qa"

        def key_for_id(self, key_id):
            if key_id != "employee-qa":
                raise ValueError("Unknown QA encryption key")
            return key

    async def verify(credential):
        return hmac.compare_digest(credential.reveal()["api_key"], private["fixture_api_key"])

    policies = {
        "procurement": IntegrationCredentialVerifierPolicy(capabilities=("procurement.place_order",), verify=verify),
    }
    if manifest.get("phone_fixture"):
        from scripts.verification.ai_employee_qa_phone import phone_test_module
        phone_fixture = phone_test_module()

        async def verify_phone(credential):
            payload = credential.reveal()
            return hmac.compare_digest(payload.get("api_key", ""), phone_fixture.API_KEY) and hmac.compare_digest(payload.get("webhook_secret", ""), phone_fixture.SECRET)

        policies["openai_realtime_sip"] = IntegrationCredentialVerifierPolicy(capabilities=("telephony.control",), verify=verify_phone)
    verifier = IntegrationCredentialProviderVerifier(provider_policies=policies)
    vault = IntegrationCredentialVaultService(key_provider=Keys(), verifier=verifier)
    manager = get_database_manager()
    if Path(evidence_name).name != evidence_name:
        raise ValueError("Fixture evidence must remain in the owned output directory")
    evidence_path = Path(manifest["output_dir"]) / evidence_name
    ledger_path = evidence_path.with_name("." + evidence_name + ".ledger")
    if evidence_path.exists() and not ledger_path.exists():
        raise RuntimeError("Fixture evidence predates restart-safe counters; prepare a fresh QA run")

    class RecordedProcurementAdapter(DeterministicProcurementTestAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.evidence_lock = asyncio.Lock()

        async def execute(self, **kwargs):
            async with self.evidence_lock:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"submitted": [], "duplicate_attempts": 0}
                digest = hashlib.sha256(kwargs["execution_key"].encode()).hexdigest()
                duplicate = digest in ledger["submitted"]
                try:
                    if duplicate:
                        ledger["duplicate_attempts"] += 1
                        raise AssertionError("duplicate provider submission across QA runtime restart")
                    return await super().execute(**kwargs)
                finally:
                    if not duplicate and kwargs["execution_key"] in self.submissions:
                        ledger["submitted"].append(digest)
                    write_json(ledger_path, ledger)
                    # Counts survive backend restarts. Only hashed execution
                    # identities are retained in the private fixture ledger.
                    write_json(evidence_path, {"provider": "deterministic procurement TEST fixture",
                        "live_provider_verified": False, "mode": self.mode,
                        "submission_count": len(ledger["submitted"]), "duplicate_attempts": ledger["duplicate_attempts"],
                        "last_action_id": kwargs["action_id"], "last_attempt_id": kwargs["attempt_id"]})

    mode = manifest.get("provider_mode", "succeeded")
    if mode not in {"succeeded", "timeout", "weak", "failed", "transient"}:
        raise ValueError("Unknown deterministic provider fixture mode")
    adapter = RecordedProcurementAdapter(mode=mode, session_factory=manager.SessionLocal)
    registry = IntegrationActionRegistry(procurement_adapter_factory=lambda: adapter)
    definition = registry.require("procurement.place_order")
    registry._definitions["procurement.place_order"] = replace(definition,
        display_name="水の注文 — deterministic TEST fixture (実注文なし)")
    return registry, vault, SemanticFixture()


async def install_fixture_connection(manager, manifest, vault):
    from src.memory.models import ExternalConnection

    root = Path(manifest["output_dir"])
    actor = {"id": manifest["fixtures"]["user_id"], "actor_type": "human", "role": "admin"}
    readiness_scope = {"owner_user_id": UUID(actor["id"]),
        "project_id": UUID(manifest["fixtures"]["project_id"]),
        "required_capabilities": ("procurement.place_order",)}
    async with manager.SessionLocal() as session:
        existing = manifest["fixtures"].get("connection_id")
        if existing:
            state = await vault.readiness_for_connection(session, UUID(existing), **readiness_scope)
            if not state["ready"]:
                raise RuntimeError("Previously seeded QA connection is no longer ready")
            return
        connection_id = uuid4()
        session.add(ExternalConnection(id=connection_id, owner_user_id=UUID(actor["id"]),
            project_id=UUID(manifest["fixtures"]["project_id"]), provider_key="procurement",
            display_name="Deterministic procurement TEST fixture — no real orders",
            remote_account_ref="employee-qa-test-account", auth_status="unknown"))
        await session.flush()
        await vault.upload_credential(session, actor, connection_id, credential_kind="api_key",
            payload={"api_key": private_state(manifest)["fixture_api_key"]}, expected_revision=0)
        await vault.verify_credential(session, actor, connection_id, expected_revision=1)
        state = await vault.readiness_for_connection(session, connection_id, **readiness_scope)
        if not state["ready"] or state["revision"] != 2:
            raise RuntimeError("Real vault fixture verification did not establish readiness")
        await session.commit()
    manifest["fixtures"]["connection_id"] = str(connection_id)
    manifest["fixture_credential"] = {"status": "verified", "revision": state["revision"],
        "state_hash": state["state_hash"], "provider": "deterministic TEST only"}
    write_json(root / "manifest.json", manifest)
