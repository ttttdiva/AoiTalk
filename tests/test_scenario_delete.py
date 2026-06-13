from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.models.ecc_models import Scenario
from src.services import scenario_service


class FakeScenarioSession:
    def __init__(self, scenario):
        self.scenario = scenario
        self.executed = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, uid):
        if model is Scenario:
            return self.scenario
        return None

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_delete_scenario_deletes_play_sessions_before_scenario(monkeypatch):
    session = FakeScenarioSession(SimpleNamespace(title="テストシナリオ"))

    async def fake_get_db_session():
        return session

    monkeypatch.setattr(scenario_service, "get_db_session", fake_get_db_session)

    assert await scenario_service.delete_scenario(str(uuid4())) is True

    assert [stmt.table.name for stmt in session.executed] == [
        "scenario_play_sessions",
        "scenarios",
    ]
    assert session.committed is True
