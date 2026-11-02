"""Finite, read-only CLI execution for one admitted native delegation phase."""

import asyncio
import concurrent.futures
import contextvars
import inspect
from pathlib import Path
from types import SimpleNamespace

from ..llm.generation_budget import GenerationBudget, current_generation_budget
from ..llm.cli_native_capabilities import _issued_native_delegation_proof
from ..llm.generation_cancellation import (register_generation_cancellation, release_generation_cancellation,
    set_current_generation_cancellation, reset_current_generation_cancellation)
from ..llm.tool_policy import (set_current_agent_tool_scope, reset_current_agent_tool_scope,
    set_current_agent_workspace_scope, reset_current_agent_workspace_scope)
from ..security.agent_run_scope import run_scope_context
from ..security.harness_execution_scope import harness_execution_scope_context
from .agent_delegation_native_scope import native_phase_scope
from .agent_dialogue_workspace import AgentDialogueWorkspaceManager
from .agent_run_service import set_current_agent_run_id, reset_current_agent_run_id
from .agent_task_invocation import _generate_within_budget
from .chat_execution_adapter import bind_chat_agent_execution, build_agent_chat_turn_client
from .session_llm_generation import cleanup_ephemeral_llm_client
from .turn_context import set_turn_context, reset_turn_context, override_turn_context
from .pricing.providers import is_cli_provider


async def invoke_native_phase_turn(*, session, service, config, claim, run_id, route, prompt,
                                   system_prompt, workspace_manager=None, client_builder=None):
    owner_loop = asyncio.get_running_loop()
    fence_context = contextvars.copy_context()
    manager = workspace_manager or AgentDialogueWorkspaceManager()
    async with native_phase_scope(session=session, service=service, config=config, claim=claim,
                                  run_id=run_id, route=route, workspace_manager=manager) as phase:
        execution, run, workspace, harness, revalidate_launch = phase
        initial_check = fence_context.copy().run(asyncio.create_task, revalidate_launch())
        _, _, execution, run, _ = await asyncio.wait_for(
            initial_check, timeout=harness.resource_limits.timeout_seconds)
        initial_proof = revalidate_launch.native_phase_proof
        if initial_proof is None or initial_proof._binding is not execution.binding:
            raise PermissionError("native phase proof was not issued for the fresh execution")
        owner, session_id, project = str(run.user_id), str(run.session_id), str(run.project_id)
        if (not route.provider or not route.model or str(route.backend).casefold() != "cli"
                or not is_cli_provider(route.provider)):
            raise ValueError("an exact CLI route is required")
        parent = current_generation_budget.get()
        budget = GenerationBudget(harness.resource_limits.timeout_seconds, parent=parent, interruptible_transport=True)
        context = set_turn_context(user_id=owner, session_id=session_id, project_id=project,
                                   agent_id=execution.agent_id, agent_revision_id=execution.agent_revision_id)
        run_token = set_current_agent_run_id(str(run_id))
        handle = register_generation_cancellation(str(run_id))
        cancel_token = set_current_generation_cancellation(handle)
        budget_token = current_generation_budget.set(budget)
        workspace_token = set_current_agent_workspace_scope(
            session_id=workspace.session_id, agent_id=workspace.agent_id, run_id=workspace.run_id,
            resource_key=workspace.resource_key, readonly=True, native_allowed=True, task_mode="read_only", edit_scope=None)
        client = None
        client_state = None
        launches = 0
        try:
            scope = workspace.native_scope
            with run_scope_context(scope), harness_execution_scope_context(harness):
                async with bind_chat_agent_execution(execution, user_id=owner, session_id=session_id,
                        project_id=project, config=config, workspace=workspace):
                    tool_token = set_current_agent_tool_scope((), tool_ids=(), delegate_policy={})
                    try:
                        source = SimpleNamespace(config=config, current_session_id=session_id, session_user_id=owner,
                            current_project_id=project, current_include_project_context=False, external_persistence_enabled=False)
                        builder = client_builder or build_agent_chat_turn_client
                        client = builder(source, config, execution, workspace=workspace, session_id=session_id,
                                         project_id=project, run_id=str(run_id))
                        if inspect.isawaitable(client):
                            client = await client
                        if (getattr(client, "provider_label", route.provider) != route.provider
                                or getattr(client, "model_name", route.model) != route.model):
                            raise ValueError("native client route does not match pinned route")
                        client_state = {name: (name in vars(client), getattr(client, name, None)) for name in
                                        ("_agent_execution_binding", "_agent_native_scope_proof", "_agent_native_phase_required",
                                         "_bind_fresh_native_provider_entry", "_agent_run_id", "current_session_id",
                                         "session_user_id", "current_project_id", "_agent_native_usage_incomplete")}
                        client._agent_execution_binding = execution.binding
                        client._agent_native_scope_proof = initial_proof
                        client._agent_native_phase_required = True
                        initial_proof.assert_context(client)
                        client.current_session_id, client.session_user_id, client.current_project_id = session_id, owner, project
                        client._agent_run_id = str(run_id)
                        if not callable(getattr(client, "_bind_fresh_native_provider_entry", None)):
                            raise RuntimeError("native CLI launch binder unavailable")
                        def fenced_bind():
                            nonlocal launches
                            try:
                                running = asyncio.get_running_loop()
                            except RuntimeError:
                                running = None
                            if running is owner_loop:
                                raise RuntimeError("native CLI launch hook called on owner loop")
                            from .turn_context import get_turn_context
                            from ..security.agent_run_scope import get_current_run_scope
                            from .agent_delegation_native_scope import NativeDelegationScopeProof
                            retained = getattr(client, "_agent_native_scope_proof", None)
                            if (type(retained) is not NativeDelegationScopeProof
                                    or retained is not _issued_native_delegation_proof.get()
                                    or retained.workspace is not workspace
                                    or getattr(client, "_agent_dialogue_workspace", None) is not workspace
                                    or getattr(client, "_agent_execution_binding", None) is not retained._binding
                                    or getattr(get_turn_context(), "agent_execution_binding", None) is not retained._binding
                                    or get_current_run_scope() is not scope
                                    or Path(workspace.working_directory).resolve() != Path(scope.canonical_root).resolve()):
                                raise PermissionError("native phase launch context changed")
                            budget.checkpoint()
                            future = fence_context.copy().run(asyncio.run_coroutine_threadsafe, revalidate_launch(), owner_loop)
                            try:
                                while True:
                                    remaining = budget.checkpoint()
                                    try:
                                        fresh = future.result(timeout=min(remaining, 0.05))
                                        break
                                    except concurrent.futures.TimeoutError:
                                        if future.done():
                                            future.result()
                            except BaseException:
                                future.cancel()
                                raise
                            budget.checkpoint()
                            fresh_execution = fresh[2]
                            proof = revalidate_launch.native_phase_proof
                            if proof is None or proof._binding is not fresh_execution.binding:
                                raise PermissionError("native phase proof does not match fresh execution")
                            token = override_turn_context(agent_execution_binding=fresh_execution.binding)
                            previous_binding = client._agent_execution_binding
                            previous_proof = client._agent_native_scope_proof
                            client._agent_execution_binding = fresh_execution.binding
                            client._agent_native_scope_proof = proof
                            proof_token = _issued_native_delegation_proof.set(proof)
                            try:
                                proof.assert_context(client)
                                from ..llm.cli_native_capabilities import _native_delegation_phase_proof
                                if _native_delegation_phase_proof(client) is not proof:
                                    raise PermissionError("fresh native phase controls are invalid")
                            except BaseException:
                                _issued_native_delegation_proof.reset(proof_token)
                                client._agent_execution_binding = previous_binding
                                client._agent_native_scope_proof = previous_proof
                                reset_turn_context(token)
                                raise
                            def reset_phase(_unused):
                                _issued_native_delegation_proof.reset(proof_token)
                                client._agent_execution_binding = previous_binding
                                client._agent_native_scope_proof = previous_proof
                                reset_turn_context(token)
                            def no_reset(_unused):
                                pass
                            launches += 1
                            return (reset_phase, None, no_reset, None, no_reset, None, previous_proof, workspace)
                        client._bind_fresh_native_provider_entry = fenced_bind
                        issued_token = _issued_native_delegation_proof.set(initial_proof)
                        try:
                            text = await _generate_within_budget(budget, handle,
                                lambda: client.generate_native_delegation_phase_async(prompt, system_prompt=system_prompt))
                        finally:
                            _issued_native_delegation_proof.reset(issued_token)
                        # The CLI ledger already aggregates provider receipts.  The
                        # last receipt is not an additional usage record.
                        usage = getattr(client, "_agent_run_usage", None)
                        evidence = {"route": {"provider": route.provider, "model": route.model},
                                    "usage": dict(usage) if isinstance(usage, dict) and usage else None,
                                    "outer_launch_count": launches, "internal_model_request_count": None}
                        if getattr(client, "_agent_native_usage_incomplete", False):
                            evidence["token_usage_state"] = "partial" if evidence["usage"] else "unknown"
                        return text, evidence
                    finally:
                        try:
                            if client is not None:
                                if client_state is not None:
                                    for name, (had, value) in client_state.items():
                                        if had:
                                            setattr(client, name, value)
                                        elif name in vars(client):
                                            delattr(client, name)
                                await cleanup_ephemeral_llm_client(client)
                        finally:
                            reset_current_agent_tool_scope(tool_token)
        finally:
            reset_current_agent_workspace_scope(workspace_token)
            current_generation_budget.reset(budget_token)
            reset_current_generation_cancellation(cancel_token)
            release_generation_cancellation(handle)
            reset_current_agent_run_id(run_token)
            reset_turn_context(context)
