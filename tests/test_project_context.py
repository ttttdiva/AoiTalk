from src.services.project_context import (
    build_project_context,
    format_project_context_for_chat_prompt,
    format_project_context_for_prompt,
    merge_project_metadata,
    normalize_project_metadata,
    sanitize_project_context_for_chat,
)


def test_normalize_project_metadata_supports_legacy_flat_fields():
    normalized = normalize_project_metadata(
        {
            "workspace_root": r"D:\Sigoto\ProjectA",
            "wbs_file": r"_projects\project_proj-1\management\wbs.md",
            "task_rules": {
                "auto_create_followup": False,
            },
            "extra_field": "keep-me",
        }
    )

    assert normalized["schema_version"] == 1
    assert normalized["links"]["workspace_root"] == "D:/Sigoto/ProjectA"
    assert normalized["management"]["wbs_file"] == "management/wbs.md"
    assert normalized["management"]["task_rules"]["auto_create_followup"] is False
    assert normalized["management"]["task_rules"]["auto_create_due_task"] is False
    assert normalized["extra_field"] == "keep-me"


def test_merge_project_metadata_preserves_existing_values():
    existing = normalize_project_metadata(
        {
            "links": {
                "workspace_root": "D:/Sigoto/ProjectA",
            },
            "management": {
                "wbs_file": "management/wbs.md",
            },
        }
    )

    merged = merge_project_metadata(
        existing,
        {
            "management": {
                "task_rules": {
                    "auto_create_due_task": True,
                }
            },
        },
    )

    assert merged["links"]["workspace_root"] == "D:/Sigoto/ProjectA"
    assert merged["management"]["wbs_file"] == "management/wbs.md"
    assert merged["management"]["task_rules"]["auto_create_due_task"] is True


def test_build_project_context_and_prompt_format():
    project = {
        "id": "proj-1",
        "name": "ProjectA",
        "slug": "project-a",
        "description": "description",
        "metadata": {
            "links": {
                "workspace_root": "D:/Sigoto/ProjectA",
            },
            "management": {
                "issue_file": "management/issues.md",
            },
        },
    }

    context = build_project_context(project)
    prompt = format_project_context_for_prompt(context)

    assert context["workspace_root"] == "D:/Sigoto/ProjectA"
    assert "project_name: ProjectA" in prompt
    assert "issue_file: management/issues.md" in prompt


def test_chat_project_context_omits_unset_management_fields():
    context = build_project_context(
        {
            "id": "proj-1",
            "name": "ProjectA",
            "slug": "project-a",
            "description": "description",
            "metadata": {},
        }
    )

    prompt = format_project_context_for_chat_prompt(context)
    sanitized = sanitize_project_context_for_chat(context)

    assert "project_name: ProjectA" in prompt
    assert "project_description: description" in prompt
    assert "workspace_root" not in prompt
    assert "wbs_file" not in prompt
    assert "status_file" not in prompt
    assert "not_set" not in prompt
    assert sanitized == {
        "id": "proj-1",
        "name": "ProjectA",
        "slug": "project-a",
        "description": "description",
    }


def test_full_project_context_keeps_management_fields_for_project_tools():
    context = build_project_context(
        {
            "id": "proj-1",
            "name": "ProjectA",
            "slug": "project-a",
            "metadata": {},
        }
    )

    prompt = format_project_context_for_prompt(context)

    assert "project_files: _projects/project_proj-1" in prompt
    assert "wbs_file: not_set" in prompt
    assert "issue_file: not_set" in prompt
    assert "risk_file: not_set" in prompt
