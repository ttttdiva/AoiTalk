"""Phone UI fixture: real vault/authority, explicit fake provider and directory."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from uuid import UUID, uuid4

from scripts.verification.ai_employee_qa import REPO, write_json


def phone_test_module():
    name = "_employee_qa_telephone_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "tests/test_telephony_service.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


async def install_phone_fixture(manager, config, manifest, vault):
    from src.memory.models import ExternalConnection
    from src.services.agent_identity_service import AgentIdentityService
    from src.services.telephony_service import TelephonyService

    fixture = phone_test_module()
    root = Path(manifest["output_dir"])
    ids = manifest["fixtures"]
    actor = {"id": ids["user_id"], "actor_type": "human", "role": "admin"}
    if not ids.get("phone_connection_id"):
        connection_id = uuid4()
        async with manager.SessionLocal() as session:
            session.add(ExternalConnection(id=connection_id, owner_user_id=UUID(ids["user_id"]),
                project_id=UUID(ids["project_id"]), provider_key="openai_realtime_sip",
                display_name="Phone TEST fixture — no PSTN/SIP", remote_account_ref="phone-test-account"))
            await session.flush()
            await vault.upload_credential(session, actor, connection_id, credential_kind="api_key",
                payload={"api_key": fixture.API_KEY, "webhook_secret": fixture.SECRET}, expected_revision=0)
            await vault.verify_credential(session, actor, connection_id, expected_revision=1)
            readiness = await vault.readiness_for_connection(session, connection_id, owner_user_id=UUID(ids["user_id"]),
                project_id=UUID(ids["project_id"]), required_capabilities=("telephony.control",))
            if not readiness["ready"]:
                raise RuntimeError("Real phone fixture credential verification failed")
            await session.commit()
        service = AgentIdentityService(manager, config=config)
        agent = await service.create_agent(display_name="電話受付 QA TEST", idempotency_key=manifest["run_id"] + ":phone",
            actor_user_id=ids["user_id"])
        revision = await service.create_revision(agent_id=agent["id"], display_name="電話受付 QA TEST",
            mission="Safe deterministic phone reception fixture", agent_team_id="employee", execution_profile_id="realtime",
            allowed_subagent_ids=["employee_operator"], capability_ceiling=["telephony_control"],
            idempotency_key=manifest["run_id"] + ":phone", actor_user_id=ids["user_id"])
        await service.upsert_organization_profile(agent_id=agent["id"], actor_user_id=ids["user_id"],
            payload={"primary_space_id": ids["space_id"], "autonomy_level": "bounded", "employment_state": "active"})
        await service.create_space_assignment(agent_id=agent["id"], space_id=ids["space_id"], assignment_kind="primary", actor_user_id=ids["user_id"])
        await service.create_project_grant(agent_id=agent["id"], project_id=ids["project_id"], role="member",
            permissions={"read": True, "write": True}, actor_user_id=ids["user_id"])
        await service.transition_agent(agent["id"], "active", expected_state="draft", actor_user_id=ids["user_id"])
        ids.update(phone_connection_id=str(connection_id), phone_agent_id=agent["id"], phone_revision_id=revision["id"])
        write_json(root / "manifest.json", manifest)

    class ObservedPhoneProvider(fixture.Provider):
        async def _command(self, operation, call_id, api_key, payload):
            try:
                return await super()._command(operation, call_id, api_key, payload)
            finally:
                write_json(root / "phone-provider-evidence.json", {"provider": "TEST fixture", "live_sip_verified": False,
                    "command_count": len(self.requests), "operations": [request[0] for request in self.requests]})

    provider = ObservedPhoneProvider()
    if not (root / "phone-provider-evidence.json").exists():
        write_json(root / "phone-provider-evidence.json", {"provider": "TEST fixture", "live_sip_verified": False,
            "command_count": 0, "operations": []})
    return TelephonyService(manager, config=config, vault=vault, provider=provider,
        directory={"main": {"display_name": "Main phone TEST", "incoming_uri": "sip:+14155550000@example.test",
            "connection_id": ids["phone_connection_id"], "destinations": {
                "sales": {"uri": "tel:+14155550001", "display_name": "Sales TEST"}}}}, runtime_factory=fixture.Runtime)
