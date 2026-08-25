"""Provider-neutral model tool exposure rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable

from ..tools.registry import ToolRegistry
from ..services.turn_context import get_turn_context
from .generation_policy import GenerationProfile, get_client_generation_policy
from .planning_policy import (
    PlanningRunPhase,
    get_current_planning_run_state,
    is_planning_cancelled_terminal,
    is_planning_phase_active,
)
from .tool_packs import (
    ToolPackSession,
    auto_load_contextual_packs,
    auto_load_packs_for_command_capabilities,
    auto_load_packs_for_tool_names,
    build_load_tool_pack_tool,
    contextual_agent_team_scope,
    tool_pack_session_for_client,
    tool_visible_for_session,
)
from .tool_policy import (
    DOCS_READ_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    command_capabilities_from_text,
    get_current_user_input,
    sanitize_command_capabilities,
)

REVIEW_TOOL_ALLOWLIST = frozenset(
    {
        *DOCS_READ_TOOL_NAMES,
        *FILESYSTEM_READ_TOOL_NAMES,
        *PROJECT_MANAGEMENT_READ_TOOL_NAMES,
        *SEARCH_TOOL_NAMES,
        "knowledge_search",
        "knowledge_read",
        "knowledge_status",
        "search_past_chats",
        "list_chat_sessions",
        "read_chat_session",
        "webex_list_selected_spaces",
        "webex_search_messages",
        "webex_get_thread",
        "get_current_time",
        "get_weather_info",
        "calculate",
    }
)

PLANNING_TOOL_ALLOWLIST = frozenset(
    {
        *REVIEW_TOOL_ALLOWLIST,
        "ask_user_question",
        "submit_plan_for_approval",
    }
)


_STORY_CONTEXT_UNSET = object()


@dataclass(frozen=True)
class ToolExposureContext:
    """Immutable request-local Story and Agent Team activation snapshot."""

    story_context: Any
    scope: Mapping[str, Any]
    story_resolution_failed: bool = False


def resolve_story_chat_context_for_client(client: Any) -> tuple[Any, bool]:
    """Resolve trusted Story context once for one provider exposure pass.

    Providers use their existing async bridge to run the lightweight durable
    ``StoryWritingSession`` resolver.  It returns only activation metadata
    (never the large Story prompt); no user prose is inspected.  The boolean
    distinguishes a resolver exception from a normal non-Story ``None``.
    """

    # The immutable server-bound TurnContext wins over a mutable/shared client
    # attribute.  This prevents a fixed provider instance serving concurrent
    # Story and General turns from resolving the wrong conversation.
    turn_session_id = str(getattr(get_turn_context(), "session_id", None) or "").strip()
    session_id = turn_session_id or str(
        getattr(client, "current_session_id", None) or ""
    ).strip()
    runner = (
        getattr(client, "_run_async_sync", None)
        or getattr(client, "_run_async_in_new_loop", None)
        or getattr(client, "_run_sync", None)
    )
    if session_id and callable(runner):
        try:
            # Provider getters retain their compatibility catch-to-None
            # behavior for prompt construction.  Tool exposure deliberately
            # bypasses them and calls the strict durable resolver so DB/ACL
            # errors remain distinguishable from an ordinary non-Story turn.
            from ..services.story_chat_context import (
                resolve_story_workflow_context_strict,
            )

            return runner(resolve_story_workflow_context_strict(session_id)), False
        except Exception:  # noqa: BLE001 - caller must fail closed
            return None, True

    # Lightweight/legacy callers without an async bridge can still provide a
    # trusted pre-resolved context getter.  This compatibility branch is not
    # used by the real Manager/Gemini/CLI provider classes above.
    getter = getattr(client, "_get_story_chat_context_sync", None)
    if callable(getter):
        try:
            return getter(), False
        except Exception:  # noqa: BLE001 - caller must fail closed
            return None, True
    return None, False


def resolve_tool_exposure_context(
    client: Any,
    *,
    story_context: Any = _STORY_CONTEXT_UNSET,
    story_resolution_failed: bool | None = None,
    project_context: dict[str, Any] | None = None,
) -> ToolExposureContext:
    """Resolve Story + canonical Team activation at most once per exposure."""

    if story_context is _STORY_CONTEXT_UNSET:
        story_context, resolved_failed = resolve_story_chat_context_for_client(client)
        if story_resolution_failed is None:
            story_resolution_failed = resolved_failed
    elif story_resolution_failed is None:
        story_resolution_failed = False
    scope = contextual_agent_team_scope(
        getattr(client, "config", None),
        client=client,
        project_context=project_context,
        story_context=story_context,
    )
    # The resolver returns fresh lists for human-readable metadata.  Freeze
    # those containers before handing the snapshot to every deferred-pack and
    # delegate specialization call so no provider helper can mutate the
    # request-local activation result while another task reuses its registry.
    frozen_scope = {
        key: tuple(value) if isinstance(value, (list, set, frozenset)) else value
        for key, value in scope.items()
    }
    return ToolExposureContext(
        story_context=story_context,
        scope=MappingProxyType(frozen_scope),
        story_resolution_failed=bool(story_resolution_failed),
    )


def tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def _tool_owner(tool: Any, owner_lookup: Callable[[str], str]) -> str:
    if isinstance(tool, str):
        return owner_lookup(tool)
    owner = str(getattr(tool, "owner", "") or "")
    return owner or owner_lookup(tool_name(tool))


def _owner_lookup_for_client(client: Any) -> Callable[[str], str]:
    registry = getattr(client, "_tool_registry", None)
    getter = getattr(registry, "get", None) if registry is not None else None
    if not callable(getter):
        return lambda name: ""

    def _lookup(name: str) -> str:
        try:
            tool = getter(name)
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(tool, "owner", "") or "")

    return _lookup


def effective_tool_pack_session(
    client: Any,
    *,
    exposure: ToolExposureContext | None = None,
) -> ToolPackSession:
    """クライアントのロード済み pack 集合を返し、自動ロード分を反映する。

    明示的なコマンド capability は意図が確定しているため、モデルの往復を
    待たずにここで pack をロードする。
    """
    session = tool_pack_session_for_client(client)
    capabilities: set[str] = set(
        sanitize_command_capabilities(
            getattr(client, "current_command_capabilities", ()) or ()
        )
    )
    user_input = get_current_user_input()
    if user_input:
        capabilities |= command_capabilities_from_text(user_input)
    if capabilities:
        auto_load_packs_for_command_capabilities(session, capabilities)
    # Apps and App Development/Story Team packs are activated only from one
    # server-resolved structured snapshot for this exposure pass.
    exposure = exposure or resolve_tool_exposure_context(client)
    auto_load_contextual_packs(
        session,
        client=client,
        contextual_scope=exposure.scope,
    )
    return session


def apply_story_pack_auto_load(client: Any, story_chat_context: Any) -> None:
    """Story writing 文脈で許可された pack を自動ロードする。"""
    if not story_chat_context:
        return
    allowed = getattr(story_chat_context, "allowed_tools", None) or frozenset()
    auto_load_packs_for_tool_names(tool_pack_session_for_client(client), allowed)


def filter_tools_for_client(
    client: Any,
    tools: Iterable[Any],
    *,
    story_context: Any = _STORY_CONTEXT_UNSET,
    story_resolution_failed: bool | None = None,
    exposure: ToolExposureContext | None = None,
) -> list[Any]:
    """Return only tools that may be shown for the client's current session."""
    values = list(tools)
    # StoryWritingSession is a server-resolved workflow boundary, not a prompt
    # hint.  Enforce its narrow allow-list here as the provider-neutral final
    # filter so legacy providers (OpenAI-compatible, Ollama, SGLang) cannot
    # publish General/App/Apps schemas even when they do not use AgentSetup.
    exposure = exposure or resolve_tool_exposure_context(
        client,
        story_context=story_context,
        story_resolution_failed=story_resolution_failed,
    )
    if exposure.story_resolution_failed:
        # A Story resolver failure must not silently widen the schema to normal
        # tools.  The durable resolver itself returns None for ordinary
        # non-Story sessions, which remains distinct from this error state.
        return []
    if is_planning_cancelled_terminal():
        return []
    story_context = exposure.story_context
    if story_context:
        allowed_story_tools = {
            str(name).strip()
            for name in (getattr(story_context, "allowed_tools", None) or ())
            if str(name).strip()
        }
        values = [tool for tool in values if tool_name(tool) in allowed_story_tools]
    if get_client_generation_policy(client).profile == GenerationProfile.REVIEW:
        planning_state = get_current_planning_run_state()
        if planning_state is not None and planning_state.phase in {
            PlanningRunPhase.PLANNING,
            PlanningRunPhase.AWAITING_PLAN_APPROVAL,
        }:
            review_allowlist = REVIEW_TOOL_ALLOWLIST | {
                "ask_user_question",
                "submit_plan_for_approval",
            }
            return [tool for tool in values if tool_name(tool) in review_allowlist]
        # REVIEW では allowlist が最も強いフィルタで、載っているのはすべて
        # 読み取り専用ツール。`load_tool_pack` は allowlist に無く REVIEW では
        # 公開されないため、pack の deferral を重ねると allowlist 済みの
        # 読み取りツールが恒久的に使えなくなる。REVIEW は allowlist だけを適用する。
        return [tool for tool in values if tool_name(tool) in REVIEW_TOOL_ALLOWLIST]
    planning_state = get_current_planning_run_state()
    if planning_state is not None and planning_state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }:
        return [
            tool for tool in values if tool_name(tool) in PLANNING_TOOL_ALLOWLIST
        ]
    if planning_state is not None and planning_state.phase == PlanningRunPhase.AWAITING_USER:
        return values
    session = effective_tool_pack_session(client, exposure=exposure)
    owner_lookup = _owner_lookup_for_client(client)
    registry = getattr(client, "_tool_registry", None)
    visible: list[Any] = []
    for tool in values:
        # ``load_tool_pack`` contains an enum/description generated when the
        # registry was first built.  Rebuild that one definition per exposure
        # pass so a fixed provider registry removes the contextual ``apps``
        # option immediately after App context is turned off.
        original_tool = tool
        if tool_name(tool) == "load_tool_pack" and registry is not None:
            try:
                dynamic_loader = build_load_tool_pack_tool(
                    registry,
                    session,
                    client=client,
                    contextual_scope=exposure.scope,
                )
            except Exception:  # noqa: BLE001 - compatibility fake registries
                dynamic_loader = None
            if dynamic_loader is None:
                continue
            # Name-only callers (CLI/tool menus) expect strings back; use the
            # rebuilt definition for gating but retain their original shape.
            if not isinstance(tool, str):
                tool = dynamic_loader
        if tool_visible_for_session(
            session,
            tool_name(tool),
            _tool_owner(tool, owner_lookup),
            client=client,
            contextual_scope=exposure.scope,
        ):
            # The Agent Team delegate is the one contextual schema whose
            # roster text must change with each request.  Clone the definition
            # rather than mutating a shared registry entry; concurrent clients
            # can then expose different Team/Subagent sets safely.
            if tool_name(tool) == "agent_team_delegate":
                try:
                    from .runtime_tool_registry import (
                        specialize_agent_team_delegate_definition,
                    )

                    # CLI menus and a few legacy adapters pass only tool
                    # names. Resolve the canonical definition solely for
                    # specialization/gating, then preserve the name-only
                    # return shape. Never keep an unverified build-time
                    # roster when the definition cannot be resolved.
                    specialization_target = tool
                    if isinstance(tool, str):
                        get_tool = getattr(registry, "get", None)
                        specialization_target = (
                            get_tool(tool) if callable(get_tool) else None
                        )
                        if specialization_target is None:
                            continue
                    tool = specialize_agent_team_delegate_definition(
                        specialization_target,
                        config=getattr(client, "config", None),
                        client=client,
                        contextual_scope=exposure.scope,
                    )
                except Exception:  # noqa: BLE001 - never leak stale roster
                    # A shared registry may contain a build-time App/Story
                    # roster.  Keeping that base definition after a failed
                    # request-local specialization would expose inactive
                    # Teams/Subagents, so drop the delegate fail-closed.
                    continue
            visible.append(original_tool if isinstance(original_tool, str) else tool)
    return visible


def is_review_generation(client: Any) -> bool:
    return get_client_generation_policy(client).profile == GenerationProfile.REVIEW


def filtered_registry_for_client(
    client: Any,
    registry: ToolRegistry,
    *,
    story_context: Any = _STORY_CONTEXT_UNSET,
    story_resolution_failed: bool | None = None,
    exposure: ToolExposureContext | None = None,
) -> ToolRegistry:
    """Create a non-expanding registry view for model exposure and execution."""
    get_all = getattr(registry, "get_all", None)
    if callable(get_all):
        values = list(get_all())
    elif isinstance(registry, dict):
        values = list(registry.values())
    else:
        get_names = getattr(registry, "get_names", None)
        get_tool = getattr(registry, "get", None)
        values = (
            [
                tool
                for name in get_names()
                if (tool := get_tool(name)) is not None
            ]
            if callable(get_names) and callable(get_tool)
            else []
        )
    filtered = filter_tools_for_client(
        client,
        values,
        story_context=story_context,
        story_resolution_failed=story_resolution_failed,
        exposure=exposure,
    )
    # 取り出せない形のレジストリ（テスト用のフェイクなど）は values が空になるため、
    # 絞り込みが発生しない場合は元のレジストリをそのまま返す。
    # A same-sized result can still contain request-local clones (notably the
    # Agent Team delegate roster, or a rebuilt contextual pack loader).  Keep
    # the original registry only when every definition is the exact object
    # that was supplied; otherwise the provider would silently retain a stale
    # shared description.
    if len(filtered) == len(values) and all(
        exposed is original for exposed, original in zip(filtered, values)
    ):
        return registry
    result = ToolRegistry()
    for tool in filtered:
        result.register(tool)
    return result
