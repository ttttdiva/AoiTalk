from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch

from src.agents.project_management_agent import ProjectManagementAgent
from src.agents.project_management_agent import _project_reference_match_score
from src.agents.project_management_agent import _normalize_task_schedule_inputs
from src.memory.project_repository import ProjectRepository
from src.memory.project_repository import (
    legacy_user_default_space_slug,
    user_inbox_project_slug,
    user_inbox_space_slug,
)
from src.services.task_management_service import (
    TaskManagementError,
    TaskManagementService,
    _get_user_task_notifications_default_enabled,
    _is_date_only_occurrence,
    _normalize_member_permissions,
    build_occurrence_schedule,
    build_time_report,
)


def test_build_occurrence_schedule_expands_daily_recurrence():
    occurrences = build_occurrence_schedule(
        start_at=datetime(2026, 3, 19, 9, 0, 0),
        end_at=datetime(2026, 3, 19, 10, 0, 0),
        recurrence_rrule="FREQ=DAILY;INTERVAL=1",
        horizon_days=3,
        base_now=datetime(2026, 3, 19, 8, 0, 0),
    )

    assert [item.start_at.isoformat() for item in occurrences] == [
        "2026-03-19T09:00:00",
        "2026-03-20T09:00:00",
        "2026-03-21T09:00:00",
    ]
    assert all(item.is_generated for item in occurrences)


def test_date_only_occurrences_do_not_count_as_notification_schedules():
    task = SimpleNamespace(all_day=False)
    all_day_task = SimpleNamespace(all_day=True)

    assert _is_date_only_occurrence(
        SimpleNamespace(
            all_day=True,
            start_at=datetime(2026, 5, 7, 15, 0, 0),
            end_at=datetime(2026, 5, 7, 15, 0, 0),
        ),
        task,
    )
    assert _is_date_only_occurrence(
        SimpleNamespace(
            all_day=False,
            start_at=datetime(2026, 5, 7, 9, 0, 0),
            end_at=datetime(2026, 5, 7, 9, 30, 0),
        ),
        all_day_task,
    )
    assert _is_date_only_occurrence(
        SimpleNamespace(
            all_day=False,
            start_at=None,
            end_at=datetime(2026, 5, 7, 0, 0, 0),
        ),
        task,
    )
    assert not _is_date_only_occurrence(
        SimpleNamespace(
            all_day=False,
            start_at=datetime(2026, 5, 7, 13, 30, 0),
            end_at=datetime(2026, 5, 7, 14, 30, 0),
        ),
        task,
    )


def test_task_notifications_default_enabled_defaults_to_on():
    assert _get_user_task_notifications_default_enabled(None) is True
    assert (
        _get_user_task_notifications_default_enabled(
            SimpleNamespace(user_settings={})
        )
        is True
    )


def test_task_notifications_default_enabled_accepts_explicit_off():
    assert (
        _get_user_task_notifications_default_enabled(
            SimpleNamespace(
                user_settings={"task_notifications_default_enabled": False}
            )
        )
        is False
    )


def test_build_time_report_aggregates_projects_days_users_and_tasks():
    report = build_time_report(
        [
            {
                "project_id": "p1",
                "project_name": "Alpha",
                "task_id": "t1",
                "task_title": "Spec",
                "user_id": "u1",
                "display_name": "Aoi",
                "started_at": datetime(2026, 3, 19, 9, 0, 0),
                "ended_at": datetime(2026, 3, 19, 10, 30, 0),
            },
            {
                "project_id": "p2",
                "project_name": "Beta",
                "task_id": "t2",
                "task_title": "Build",
                "user_id": "u2",
                "display_name": "Ren",
                "started_at": datetime(2026, 3, 20, 13, 0, 0),
                "ended_at": datetime(2026, 3, 20, 15, 0, 0),
            },
        ]
    )

    assert report["summary"] == {
        "total_seconds": 12600,
        "entry_count": 2,
        "active_entries": 0,
    }
    assert report["by_project"][0]["label"] == "Beta"
    assert report["by_project"][0]["seconds"] == 7200
    assert report["by_day"][0]["key"] == "2026-03-19"
    assert report["by_day"][1]["key"] == "2026-03-20"
    assert {bucket["label"] for bucket in report["by_user"]} == {"Aoi", "Ren"}
    assert {bucket["label"] for bucket in report["by_task"]} == {"Spec", "Build"}


def test_project_management_agent_exposes_task_tools():
    agent = ProjectManagementAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "required"
    assert {
        "get_project_context",
        "list_projects",
        "list_tasks",
        "create_task",
        "update_task",
        "delete_task",
        "assign_task",
        "schedule_task",
        "archive_project_info_category",
        "delete_project_document",
        "delete_project_fact",
        "update_record_row",
        "delete_record_rows",
        "delete_record_table",
        "start_timer",
        "stop_timer",
        "log_time",
        "list_calendar",
        "get_time_report",
    }.issubset(tool_names)


def test_project_management_agent_guides_reservation_task_titles():
    agent = ProjectManagementAgent(model="gpt-4o-mini").agent
    create_task_tool = next(tool for tool in agent.tools if tool.name == "create_task")
    title_description = create_task_tool.params_json_schema["properties"]["title"][
        "description"
    ]
    details_description = create_task_tool.params_json_schema["properties"][
        "description"
    ]["description"]

    assert "予約先 来店（サービス名）" in agent.instructions
    assert "Do not include appointment dates or times in the title" in agent.instructions
    assert "Never append parenthesized dates/times" in agent.instructions
    assert "予約確認タスク" in agent.instructions
    assert "do not include appointment dates/times" in title_description
    assert "parenthesized dates/times" in title_description
    assert "reservation number" in title_description
    assert "reservation number" in details_description


def test_project_management_agent_guides_date_only_task_schedules():
    agent = ProjectManagementAgent(model="gpt-4o-mini").agent
    create_task_tool = next(tool for tool in agent.tools if tool.name == "create_task")
    properties = create_task_tool.params_json_schema["properties"]

    assert "予定日" in agent.instructions
    assert "Do not mention a schedule" in agent.instructions
    assert "due_date" in properties
    assert "all_day" in properties
    assert "予定日" in properties["due_date"]["description"]


def test_project_management_agent_records_conversation_project_facts():
    agent = ProjectManagementAgent(model="gpt-4o-mini").agent
    upsert_fact_tool = next(tool for tool in agent.tools if tool.name == "upsert_project_fact")
    properties = upsert_fact_tool.params_json_schema["properties"]

    assert "deferred project fact reflection" in agent.instructions
    assert "inspect existing project information" in agent.instructions
    assert "Create a new fact only when it is genuinely new" in agent.instructions
    assert 'source_type="conversation"' in agent.instructions
    assert "Preserve uncertainty" in agent.instructions
    assert "confidence" in properties


def test_date_only_due_date_normalizes_to_all_day_task_range():
    start_at, end_at, all_day = _normalize_task_schedule_inputs(
        due_date="2026-05-30"
    )

    assert start_at == datetime(2026, 5, 30, 0, 0)
    assert end_at == datetime(2026, 5, 31, 0, 0)
    assert all_day is True


def test_start_at_with_time_keeps_explicit_schedule_shape():
    start_at, end_at, all_day = _normalize_task_schedule_inputs(
        start_at="2026-05-30T13:30:00",
        end_at="2026-05-30T14:30:00",
    )

    assert start_at == datetime(2026, 5, 30, 13, 30)
    assert end_at == datetime(2026, 5, 30, 14, 30)
    assert all_day is False


def test_project_reference_match_accepts_project_suffix_and_metadata_alias():
    project = {
        "id": "project-id",
        "name": "ExampleCorp Firewall導入",
        "slug": "example-fw",
        "aliases": [],
        "metadata": {"aliases": ["example"]},
    }

    assert _project_reference_match_score(project, "ExampleCorp Firewall導入プロジェクト") == 100
    assert _project_reference_match_score(project, "example") == 100


def test_project_reference_match_ignores_whitespace_and_case():
    project = {
        "id": "project-id",
        "name": "ExampleCorp Firewall",
        "slug": "example-fw",
        "aliases": ["EXAMPLE-FW"],
        "metadata": {},
    }

    assert _project_reference_match_score(project, "examplecorp firewall project") == 100
    assert _project_reference_match_score(project, "example-fw") == 100


def test_get_user_inbox_project_id_returns_scalar_when_present():
    session = AsyncMock()
    execute_result = AsyncMock()
    execute_result.scalar_one_or_none = lambda: "project-uuid"
    session.execute = AsyncMock(return_value=execute_result)

    async def run():
        return await ProjectRepository.get_user_inbox_project_id(session, uuid4())

    assert asyncio.run(run()) == "project-uuid"


def test_get_user_inbox_project_id_returns_none_when_absent():
    session = AsyncMock()
    execute_result = AsyncMock()
    execute_result.scalar_one_or_none = lambda: None
    session.execute = AsyncMock(return_value=execute_result)

    async def run():
        return await ProjectRepository.get_user_inbox_project_id(session, uuid4())

    assert asyncio.run(run()) is None


def test_user_inbox_slugs_are_stable():
    user_id = uuid4()

    assert user_inbox_space_slug(user_id) == f"inbox-{user_id}"
    assert user_inbox_project_slug(user_id) == f"inbox-project-{user_id}"
    assert legacy_user_default_space_slug(user_id) == f"default-{user_id}"


def test_ensure_inbox_membership_repairs_inbox_before_lookup():
    user_id = uuid4()
    inbox_id = uuid4()
    session = AsyncMock()

    async def run():
        service = TaskManagementService()
        with (
            patch.object(ProjectRepository, "ensure_user_inbox_setup", AsyncMock()),
            patch.object(
                ProjectRepository,
                "get_user_inbox_project_id",
                AsyncMock(return_value=inbox_id),
            ),
            patch.object(ProjectRepository, "get_member", AsyncMock(return_value=object())),
        ):
            resolved = await service._ensure_inbox_membership(session, user_id)
            ProjectRepository.ensure_user_inbox_setup.assert_awaited_once_with(
                session, user_id
            )
            return resolved

    assert asyncio.run(run()) == inbox_id


def test_normalize_member_permissions_accepts_json_string():
    permissions = _normalize_member_permissions("member", '{"read": true, "write": false}')

    assert permissions == {"read": True, "write": False}


def test_task_broadcast_failures_do_not_raise():
    async def failing_broadcaster(message):
        raise RuntimeError(f"boom: {message['type']}")

    async def run():
        service = TaskManagementService(broadcaster=failing_broadcaster)
        await service._broadcast("task_created", {"id": "123"})

    asyncio.run(run())


def test_reorder_tasks_rejects_incomplete_project_order():
    user_id = uuid4()
    project_id = uuid4()
    first_task_id = uuid4()
    second_task_id = uuid4()
    session = AsyncMock()
    result = Mock()
    result.scalars.return_value.all.return_value = [first_task_id, second_task_id]
    session.execute = AsyncMock(return_value=result)
    service = TaskManagementService()

    async def run():
        service.require_project_permission = AsyncMock(return_value=None)
        await service.reorder_tasks(
            session,
            user_id=user_id,
            project_id=project_id,
            task_ids=[first_task_id],
        )

    try:
        asyncio.run(run())
    except TaskManagementError as exc:
        assert exc.status_code == 409
        assert "every top-level task" in exc.message
    else:
        raise AssertionError("Expected TaskManagementError")
    session.commit.assert_not_called()


def test_delete_project_soft_deletes_sync_entities():
    project_id = uuid4()
    task_id = uuid4()
    project = ProjectRepository.__new__(ProjectRepository)
    project.deleted_at = None
    project.updated_at = None

    session = AsyncMock()
    task_ids_result = Mock()
    task_ids_result.all.return_value = [(task_id,)]

    async def execute_side_effect(statement):
        if getattr(statement, "is_select", False):
            froms = statement.get_final_froms()
            if any(getattr(from_obj, "name", None) == "tasks" for from_obj in froms):
                return task_ids_result
        return Mock()

    session.execute = AsyncMock(side_effect=execute_side_effect)

    def has_value_key(statement, expected_key: str) -> bool:
        values = getattr(statement, "_values", {})
        for key in values.keys():
            if getattr(key, "name", None) == expected_key:
                return True
        return False

    async def run():
        with patch.object(ProjectRepository, "get_by_id", AsyncMock(return_value=project)):
            return await ProjectRepository.delete_project(session, project_id)

    assert asyncio.run(run()) is True
    assert project.deleted_at is not None
    assert project.updated_at is not None
    session.commit.assert_awaited_once()

    statements = [call.args[0] for call in session.execute.await_args_list]
    assert any(
        getattr(statement, "is_update", False)
        and getattr(statement.table, "name", None) == "time_entries"
        and has_value_key(statement, "deleted_at")
        for statement in statements
    )
    assert any(
        getattr(statement, "is_update", False)
        and getattr(statement.table, "name", None) == "task_occurrences"
        and has_value_key(statement, "deleted_at")
        for statement in statements
    )
    assert any(
        getattr(statement, "is_update", False)
        and getattr(statement.table, "name", None) == "tasks"
        and has_value_key(statement, "deleted_at")
        for statement in statements
    )
    assert any(
        getattr(statement, "is_update", False)
        and getattr(statement.table, "name", None) == "conversation_sessions"
        and has_value_key(statement, "project_id")
        for statement in statements
    )
