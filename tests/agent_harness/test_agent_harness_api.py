from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent_harness_routes import create_agent_harness_router


def test_agent_harness_refresh_runs_even_when_global_flag_is_false(tmp_path):
    app = FastAPI()

    def require_auth():
        return None

    app.include_router(
        create_agent_harness_router(
            require_auth_dependency=require_auth,
            config={
                "agent_harness": {
                    "enabled": False,
                    "workspace_root": str(tmp_path / "workspaces"),
                    "workflow_file": str(tmp_path / "WORKFLOW.md"),
                }
            },
            get_db_manager=lambda: None,
        )
    )

    client = TestClient(app)

    state = client.get("/api/agent-harness/state")
    refresh = client.post("/api/agent-harness/refresh")

    assert state.status_code == 200
    assert state.json()["state"]["enabled"] is False
    assert refresh.status_code == 200
    assert refresh.json()["dispatched"] is True
