"""LLM function-calling entrypoint for actual Skill invocation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
from typing import Any

from ..tools.core import ToolDefinition, tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_SKILL_NAMES = {"", "?", "？", "none", "null"}


def _lookup_skill(skill_name: str) -> tuple[Any | None, str | None, str | None]:
    """Resolve one canonical Skill without emitting usage evidence."""
    normalized_name = str(skill_name or "").strip()
    if normalized_name.casefold() in _PLACEHOLDER_SKILL_NAMES:
        return (
            None,
            None,
            "有効なスキル名を指定してください。"
            "利用可能なスキルはスキル一覧または load_tool_pack の説明を確認してください。",
        )

    # ``masking`` is discoverable as a canonical Skill identity, but its
    # execution is intentionally owned by the authenticated request boundary.
    # Never let the generic function-calling tool render/receipt it (and never
    # send its source input through a model prompt).
    if normalized_name.casefold().lstrip("/") == "masking":
        return (
            None,
            None,
            "'/masking' はサーバー内蔵のローカル操作です。チャットから実行してください。",
        )

    from .loader import load_project_skills
    from .registry import get_skill_registry
    from ..services.project_context import get_runtime_project_context

    registry = get_skill_registry()
    context = get_runtime_project_context() or {}
    project_id = str(context.get("id") or "").strip() or None
    if project_id:
        load_project_skills(project_id)
    skill = registry.get_by_alias(normalized_name, project_id) or registry.get(
        normalized_name,
        project_id,
    )
    if skill is None:
        available = ", ".join(registry.get_names(project_id))
        return (
            None,
            project_id,
            f"スキル '{normalized_name}' が見つかりません。利用可能なスキル: {available}",
        )
    return skill, project_id, None


def _render_skill(skill: Any, input_text: str) -> str:
    rendered = skill.render_prompt(input_text)
    return f"[スキル: {skill.name}]\n{rendered}"


def _effective_project_id(project_id: str | None) -> str | None:
    if project_id:
        return str(project_id).strip() or None
    from ..services.turn_context import get_turn_context

    turn = get_turn_context()
    return str(getattr(turn, "project_id", "") or "").strip() or None


def skill_from_snapshot(snapshot: Any) -> Any:
    """Build one SkillDefinition from the exact canonical snapshot payload."""
    from .models import SkillDefinition, SkillTriggerMode

    payload = dict(snapshot.payload or {})
    try:
        trigger_mode = SkillTriggerMode(str(payload.get("trigger_mode") or "both"))
    except ValueError:
        trigger_mode = SkillTriggerMode.BOTH
    return SkillDefinition(
        name=snapshot.name,
        description=str(payload.get("description") or ""),
        prompt_template=str(payload.get("prompt_template") or ""),
        trigger_mode=trigger_mode,
        aliases=list(payload.get("aliases") or []),
        bound_tools=list(payload.get("bound_tools") or []),
        examples=list(payload.get("examples") or []),
        tags=list(payload.get("tags") or []),
        parameters=dict(payload.get("parameters") or {}),
        source_path=str(snapshot.path),
    )


def render_skill_snapshot(
    snapshot: Any,
    input_text: str,
    *,
    render_kwargs: dict[str, Any] | None = None,
    include_header: bool = False,
) -> str:
    """Render only from the snapshot whose bytes/hash identify this invocation."""
    renderer = skill_from_snapshot(snapshot)
    rendered = renderer.render_prompt(
        input_text,
        **dict(render_kwargs or {}),
    )
    return f"[スキル: {snapshot.name}]\n{rendered}" if include_header else rendered


def _trusted_usage_context() -> dict[str, str | None] | None:
    from ..services.agent_run_service import get_current_agent_run_id
    from ..services.turn_context import get_turn_context

    turn = get_turn_context()
    actor_id = str(getattr(turn, "user_id", "") or "").strip() or None
    message_id = str(getattr(turn, "message_id", "") or "").strip() or None
    agent_run_id = (
        str(get_current_agent_run_id() or "").strip() or None
    )
    if not actor_id or not (message_id or agent_run_id):
        return None
    return {
        "actor_id": actor_id,
        "session_id": str(getattr(turn, "session_id", "") or "").strip() or None,
        "message_id": message_id,
        "agent_run_id": agent_run_id,
        "tool_call_id": (
            str(getattr(turn, "tool_call_id", "") or "").strip() or None
        ),
        "client_message_id": (
            str(getattr(turn, "client_message_id", "") or "").strip() or None
        ),
    }


async def _record_actual_usage(
    *,
    service: Any,
    skill: Any,
    snapshot: Any,
    project_id: str | None,
    invocation_path: str,
    outcome: str,
) -> None:
    """Persist one actual invocation receipt only when trusted provenance exists."""
    trusted = _trusted_usage_context()
    if trusted is None:
        return

    try:
        await service.record_usage(
            actor_id=str(trusted["actor_id"]),
            skill=skill,
            invocation_path=invocation_path,
            outcome=outcome,
            project_id=project_id,
            session_id=trusted["session_id"],
            message_id=trusted["message_id"],
            agent_run_id=trusted["agent_run_id"],
            tool_call_id=trusted["tool_call_id"],
            client_message_id=trusted["client_message_id"],
            rendered_snapshot=snapshot,
        )
    except Exception:
        logger.exception(
            "Failed to persist Skill %s receipt",
            "usage" if outcome == "success" else "error",
        )


def _run_receipt_coroutine_sync(coro: Any) -> Any:
    """Drive receipt persistence from the synchronous JSON tool runtime."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # RegistryToolRouter.execute can be reached while its caller owns an event
    # loop.  Match ToolDefinition.execute's compatibility behaviour: execute
    # the coroutine in a worker while preserving task-local provenance.
    current_context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(current_context.run, asyncio.run, coro)
        return future.result()


def _record_actual_usage_sync(
    *,
    service: Any,
    skill: Any,
    snapshot: Any,
    project_id: str | None,
    invocation_path: str,
    outcome: str,
) -> None:
    if _trusted_usage_context() is None:
        return
    _run_receipt_coroutine_sync(
        _record_actual_usage(
            service=service,
            skill=skill,
            snapshot=snapshot,
            project_id=project_id,
            invocation_path=invocation_path,
            outcome=outcome,
        )
    )


def _prepare_exact_invocation(
    skill: Any,
    *,
    project_id: str | None,
) -> tuple[Any, Any, Any, str | None]:
    from ..services.skill_learning_service import SkillLearningService

    effective_project_id = _effective_project_id(project_id)
    service = SkillLearningService()
    snapshot = service.prepare_usage_snapshot(
        skill,
        project_id=effective_project_id,
    )
    renderer = skill_from_snapshot(snapshot)
    return service, snapshot, renderer, effective_project_id


async def invoke_resolved_skill(
    skill: Any,
    input_text: str,
    *,
    project_id: str | None = None,
    invocation_path: str = "chain",
    render_kwargs: dict[str, Any] | None = None,
    include_header: bool = False,
) -> str:
    """Render one already-resolved Skill from an exact canonical snapshot.

    Chain/API callers may run without a TurnContext; rendering still works but
    no receipt is fabricated.  When trusted provenance exists, the receipt is
    bound to the same snapshot that supplied the rendered prompt.
    """
    service, snapshot, renderer, effective_project_id = _prepare_exact_invocation(
        skill,
        project_id=project_id,
    )
    try:
        rendered = renderer.render_prompt(
            input_text,
            **dict(render_kwargs or {}),
        )
    except Exception:
        await _record_actual_usage(
            service=service,
            skill=skill,
            snapshot=snapshot,
            project_id=effective_project_id,
            invocation_path=invocation_path,
            outcome="error",
        )
        raise

    await _record_actual_usage(
        service=service,
        skill=skill,
        snapshot=snapshot,
        project_id=effective_project_id,
        invocation_path=invocation_path,
        outcome="success",
    )
    return f"[スキル: {snapshot.name}]\n{rendered}" if include_header else rendered


def _invoke_resolved_skill_sync(
    skill: Any,
    input_text: str,
    *,
    project_id: str | None,
    invocation_path: str,
    include_header: bool,
) -> str:
    service, snapshot, renderer, effective_project_id = _prepare_exact_invocation(
        skill,
        project_id=project_id,
    )
    try:
        rendered = renderer.render_prompt(input_text)
    except Exception:
        _record_actual_usage_sync(
            service=service,
            skill=skill,
            snapshot=snapshot,
            project_id=effective_project_id,
            invocation_path=invocation_path,
            outcome="error",
        )
        raise
    _record_actual_usage_sync(
        service=service,
        skill=skill,
        snapshot=snapshot,
        project_id=effective_project_id,
        invocation_path=invocation_path,
        outcome="success",
    )
    return f"[スキル: {snapshot.name}]\n{rendered}" if include_header else rendered


@tool
def invoke_skill(skill_name: str, input_text: str) -> str:
    """スキルを呼び出してプロンプトテンプレートを展開する

    Args:
        skill_name: スキル名またはエイリアス（例: translate, 翻訳）
        input_text: スキルに渡すテキスト入力

    Returns:
        展開されたスキルプロンプト
    """
    skill, _project_id, terminal = _lookup_skill(skill_name)
    if terminal is not None:
        return terminal
    assert skill is not None
    return _render_skill(skill, input_text)


def _invoke_skill_sync_runtime(skill_name: str, input_text: str) -> str:
    """Actual synchronous tool-runtime path used by the JSON tool loop."""
    skill, project_id, terminal = _lookup_skill(skill_name)
    if terminal is not None:
        return terminal
    assert skill is not None
    return _invoke_resolved_skill_sync(
        skill,
        input_text,
        project_id=project_id,
        invocation_path="invoke_skill",
        include_header=True,
    )


async def invoke_named_skill(
    skill_name: str,
    input_text: str,
    *,
    invocation_path: str = "invoke_skill",
) -> str:
    """Invoke a named Skill with the normal exact-snapshot runtime semantics.

    Non-tool runtime surfaces such as Heartbeat may supply their own canonical
    invocation path. A receipt is still emitted only when the existing
    TurnContext/AgentRun provenance is trusted; this helper never creates a
    message or AgentRun.
    """
    skill, project_id, terminal = _lookup_skill(skill_name)
    if terminal is not None:
        return terminal
    assert skill is not None
    return await invoke_resolved_skill(
        skill,
        input_text,
        project_id=project_id,
        invocation_path=invocation_path,
        include_header=True,
    )


async def _invoke_skill_async(skill_name: str, input_text: str) -> str:
    """Async tool path: render the Skill and receipt actual execution."""
    return await invoke_named_skill(
        skill_name,
        input_text,
        invocation_path="invoke_skill",
    )


class _InvokeSkillToolDefinition(ToolDefinition):
    """Keep .function pure while receipting actual sync/async executions."""

    def execute(self, **kwargs: Any) -> Any:
        normalized = self.normalize_arguments(kwargs)
        return _invoke_skill_sync_runtime(**normalized)

    async def execute_async(self, **kwargs: Any) -> Any:
        normalized = self.normalize_arguments(kwargs)
        return await self._await_with_timeout(
            _invoke_skill_async(**normalized)
        )


# ``invoke_skill.function`` is intentionally the historical synchronous
# compatibility callable and emits no receipt by itself.  Actual runtime calls
# pass through this subclass's execute/execute_async methods.
#
# Keeping the override on the ToolDefinition *class* rather than assigning an
# instance attribute is important because runtime registries use dataclass
# replacement/copy patterns; the subclass method survives those copies.
_base_invoke_skill = invoke_skill
invoke_skill = _InvokeSkillToolDefinition(
    name=_base_invoke_skill.name,
    description=_base_invoke_skill.description,
    function=_base_invoke_skill.function,
    parameters=_base_invoke_skill.parameters,
    is_async=_base_invoke_skill.is_async,
    risk=_base_invoke_skill.risk,
    side_effect=_base_invoke_skill.side_effect,
    requires_approval=_base_invoke_skill.requires_approval,
    timeout_seconds=_base_invoke_skill.timeout_seconds,
    supports_parallel=_base_invoke_skill.supports_parallel,
    owner=_base_invoke_skill.owner,
    availability=_base_invoke_skill.availability,
)
