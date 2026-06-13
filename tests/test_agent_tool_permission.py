import asyncio

from src.llm.generation_policy import (
    GenerationProfile,
    generation_policy_for_profile,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from src.tools.external_llm_permission import ExternalLLMPermissionManager


def _set_policy(profile: GenerationProfile):
    return set_current_generation_policy(
        generation_policy_for_profile(profile)
    )


def test_chat_profile_requires_mutation_permissions_only():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})
    token = _set_policy(GenerationProfile.CHAT)
    try:
        assert not manager.is_permission_required("web_search")
        assert manager.is_permission_required("edit_file")
        assert manager.is_permission_required("delete_file")
        assert manager.is_permission_required("execute_command")
        assert not manager.is_permission_required("view_file")
    finally:
        reset_current_generation_policy(token)


def test_assisted_work_profile_requires_all_configured_tool_permissions():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})
    token = _set_policy(GenerationProfile.ASSISTED_WORK)
    try:
        assert manager.is_permission_required("web_search")
        assert manager.is_permission_required("edit_file")
        assert manager.is_permission_required("delete_file")
        assert not manager.is_permission_required("view_file")
    finally:
        reset_current_generation_policy(token)


def test_autonomous_work_profile_does_not_require_permission():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": False}})
    token = _set_policy(GenerationProfile.AUTONOMOUS_WORK)
    try:
        assert not manager.is_permission_required("web_search")
        assert not manager.is_permission_required("edit_file")
        assert not manager.is_permission_required("execute_command")
    finally:
        reset_current_generation_policy(token)


def test_assisted_work_without_broadcast_denies_execution():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})
    token = _set_policy(GenerationProfile.ASSISTED_WORK)
    try:
        assert not asyncio.run(
            manager.request_permission("web_search", {"query": "AoiTalk"})
        )
    finally:
        reset_current_generation_policy(token)


def test_external_model_prompt_request_returns_edited_prompt():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})
    seen = {}

    async def broadcast(message):
        seen["message"] = message
        request_id = message["data"]["request_id"]
        manager.handle_external_model_prompt_response(
            request_id,
            True,
            "edited prompt",
        )

    manager.set_broadcast_callback(broadcast)

    result = asyncio.run(
        manager.request_external_model_prompt(
            "original prompt",
            provider="openai",
            model="gpt-4o",
        )
    )

    assert result == "edited prompt"
    assert seen["message"]["type"] == "external_model_prompt_request"
    assert seen["message"]["data"]["prompt"] == "original prompt"
    assert seen["message"]["data"]["original_prompt"] == "original prompt"


def test_external_model_prompt_request_defaults_to_redacted_prompt():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})
    seen = {}

    async def broadcast(message):
        seen["message"] = message
        request_id = message["data"]["request_id"]
        manager.handle_external_model_prompt_response(request_id, True, "")

    manager.set_broadcast_callback(broadcast)

    result = asyncio.run(
        manager.request_external_model_prompt(
            "original prompt with Customer A",
            redacted_prompt="redacted prompt with [CUSTOMER_1]",
            redaction_findings=[
                {"category": "CONFIDENTIAL_TERM", "placeholder": "[CUSTOMER_1]"}
            ],
            provider="openai",
            model="gpt-4o",
        )
    )

    assert result == "redacted prompt with [CUSTOMER_1]"
    assert seen["message"]["data"]["prompt"] == "original prompt with Customer A"
    assert seen["message"]["data"]["redacted_prompt"] == "redacted prompt with [CUSTOMER_1]"
    assert seen["message"]["data"]["redaction_findings"] == [
        {"category": "CONFIDENTIAL_TERM", "placeholder": "[CUSTOMER_1]"}
    ]


def test_external_model_prompt_without_confirmation_returns_redacted_prompt():
    manager = ExternalLLMPermissionManager({"external_llm": {"auto_approve": True}})

    result = asyncio.run(
        manager.request_external_model_prompt(
            "original prompt",
            redacted_prompt="redacted prompt",
            provider="openai",
            model="gpt-4o",
            confirm=False,
        )
    )

    assert result == "redacted prompt"
