"""会話生成の制御・ステータス・ステアリング・音声ディスパッチ・
WebSocket ペイロード正規化関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403
from concurrent.futures import Future as ConcurrentFuture
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from src.llm.generation_error import DEFAULT_GENERATION_FAILURE_MESSAGE
from src.llm.generation_cancellation import (
    abort_generation_interrupt,
    commit_generation_interrupt,
    register_generation_cancellation,
    release_generation_cancellation,
    reserve_generation_interrupt,
    request_generation_cancellation,
    reset_current_generation_cancellation,
    set_current_generation_cancellation,
)
from src.tools.external_llm_permission import (
    reset_permission_session_key,
    set_permission_session_key,
)
from ...services.turn_context import (
    get_turn_context,
    reset_turn_context,
    set_turn_context,
)


# JSON/WebSocket input cannot manufacture this object identity.  Only code
# inside the server can opt into the pre-authentication legacy compatibility
# path by passing this marker explicitly.
TRUSTED_LEGACY_MARKER = object()


class ConversationMixin:
    """WebChatServer の会話制御メソッド群。"""

    async def _set_dispatch_user_context(self, data: Dict[str, Any]) -> None:
        """Install the durable dispatch principal for agent tool execution.

        WebSocket handlers receive a request-scoped ContextVar, but queued
        REST/outbox work may resume in another task or after a process restart.
        Reconstruct the same least-privilege file context from the immutable
        dispatch payload before the generation callback is scheduled.
        """
        from ...tools.os_operations.tools import set_current_user_context

        auth_enabled = getattr(self, "auth_enabled", None)
        if auth_enabled is False:
            # This is the explicit personal/legacy compatibility mode.  The
            # authenticated Enterprise path never reaches this branch.
            # There is no durable principal to re-resolve in this mode; keep
            # the legacy admin capability explicit and do not trust a stale
            # client marker from a queued payload.
            data["_sender_is_admin"] = True
            set_current_user_context(None, True, [], [], [])
            return
        if auth_enabled is not True:
            raise PermissionError("Authentication state is unavailable")

        user_id = str(data.get("_sender_user_id") or "").strip()
        if not user_id or user_id == "default_user":
            raise PermissionError("Authenticated dispatch user identity is required")

        # The request/outbox marker is only a hint from the time the message
        # was accepted.  A user can be disabled or demoted before the queued
        # worker actually runs, so every authenticated worker must resolve the
        # current active role from the database and ignore that stale marker.
        recovered_role = await self._resolve_recovered_user_role(user_id)
        if recovered_role is None:
            raise PermissionError("Authenticated dispatch user is unavailable")
        is_admin = recovered_role == "admin"
        # ``_sender_is_admin`` may have been captured when a REST request was
        # accepted (especially the non-idempotent/no-client_message_id path).
        # Replace it at worker handoff with the role resolved from the current
        # database row so a demoted admin cannot bypass mention/project/file
        # ACLs in ``_handle_user_message``.
        data["_sender_is_admin"] = is_admin

        if is_admin:
            set_current_user_context(user_id, True, [], [], [])
            return

        from uuid import UUID

        from ...memory.database import get_database_manager
        from ...memory.project_repository import ProjectRepository
        from ...services.task_management._shared import _normalize_member_permissions

        try:
            user_uuid = UUID(user_id)
        except (TypeError, ValueError) as exc:
            raise PermissionError("Authenticated dispatch user identity is invalid") from exc

        db_manager = getattr(self, "_db_manager", None) or get_database_manager()
        db_session = await db_manager.get_session()
        try:
            projects = await ProjectRepository.get_user_projects(db_session, user_uuid)
        finally:
            await db_session.close()

        readable_project_ids: List[str] = []
        writable_project_ids: List[str] = []
        deletable_project_ids: List[str] = []
        for project in projects:
            if not isinstance(project, dict):
                continue
            project_id = str(project.get("id") or "").strip()
            if not project_id:
                continue
            owner_id = str(project.get("owner_id") or "").strip()
            membership = project.get("membership")
            membership = membership if isinstance(membership, dict) else {}
            role = str(membership.get("role") or "member")
            permissions = _normalize_member_permissions(
                role,
                membership.get("permissions"),
            )
            if owner_id == user_id or permissions.get("read") is True:
                readable_project_ids.append(project_id)
            if owner_id == user_id or permissions.get("write") is True:
                writable_project_ids.append(project_id)
            if owner_id == user_id or permissions.get("delete") is True:
                deletable_project_ids.append(project_id)

        set_current_user_context(
            user_id,
            False,
            readable_project_ids,
            writable_project_ids,
            deletable_project_ids,
        )

    async def _resolve_recovered_user_role(self, user_id: str) -> Optional[str]:
        """Resolve a durable dispatch principal from the current DB row."""
        from uuid import UUID

        from ...memory.database import get_database_manager
        from ...memory.user_repository import UserRepository

        db_session = await get_database_manager().get_session()
        try:
            try:
                principal_id = UUID(user_id)
            except (TypeError, ValueError):
                # A malformed durable identity is permanent data corruption,
                # not a transient database outage.  Returning None lets the
                # caller terminalize the delivery instead of retrying forever.
                return None
            principal = await UserRepository.get_by_id(db_session, principal_id)
            if principal is None or getattr(principal, "is_active", True) is False:
                return None
            return str(getattr(principal, "role", "") or "").casefold()
        finally:
            await db_session.close()

    async def _queue_claimed_dispatch_delivery(
        self,
        agent_run_service,
        agent_run: Dict[str, Any],
        delivery: Dict[str, Any],
    ) -> bool:
        """Queue one claimed outbox row and settle its lease exactly once."""
        agent_run_id = str(agent_run["id"])
        lease_token = str(delivery["lease_token"])
        settlement_lock = asyncio.Lock()
        settlement = {"settled": False}

        async def mark_terminal_delivery() -> bool:
            async with settlement_lock:
                if settlement["settled"]:
                    return True
                marked = await agent_run_service.mark_dispatch_delivered(
                    run_id=agent_run_id,
                    lease_token=lease_token,
                )
                if marked:
                    settlement["settled"] = True
                return bool(marked)

        async def release_failed_handoff() -> bool:
            async with settlement_lock:
                if settlement["settled"]:
                    return True
                released = await agent_run_service.release_dispatch_delivery(
                    run_id=agent_run_id,
                    lease_token=lease_token,
                )
                if released:
                    settlement["settled"] = True
                return bool(released)

        async def fail_terminal_delivery(error: str) -> bool:
            async with settlement_lock:
                if settlement["settled"]:
                    return True
                await agent_run_service.fail_run(agent_run_id, error)
                marked = await agent_run_service.mark_dispatch_delivered(
                    run_id=agent_run_id,
                    lease_token=lease_token,
                )
                if marked:
                    settlement["settled"] = True
                return bool(marked)

        async def cancel_terminal_delivery() -> bool:
            async with settlement_lock:
                if settlement["settled"]:
                    return True
                await agent_run_service.cancel_run(
                    agent_run_id,
                    message="Conversation generation stopped by user",
                )
                marked = await agent_run_service.mark_dispatch_delivered(
                    run_id=agent_run_id,
                    lease_token=lease_token,
                )
                if marked:
                    settlement["settled"] = True
                return bool(marked)

        async def renew_active_delivery() -> bool:
            if settlement["settled"]:
                return False
            return await agent_run_service.renew_dispatch_delivery(
                run_id=agent_run_id,
                lease_token=lease_token,
                lease_seconds=60.0,
            )

        if str(agent_run.get("status") or "").casefold() in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            if not await mark_terminal_delivery():
                raise RuntimeError("terminal dispatch delivery lease was lost")
            return True

        queued_payload = dict(delivery["payload"])
        for principal_key in (
            "_sender_user_id",
            "_sender_is_admin",
            "_trusted_legacy",
            "_sender_display_name",
        ):
            queued_payload.pop(principal_key, None)
        # The outbox payload is durable JSON and may predate the sender
        # identity fields.  Rebuild the principal from the immutable AgentRun
        # row during recovery; never preserve a stale client/old-payload admin
        # flag.  Project/App authorization resolves the current role from the
        # database when this flag is absent.
        if getattr(self, "auth_enabled", True) is False:
            queued_payload["_trusted_legacy"] = TRUSTED_LEGACY_MARKER
        else:
            durable_user_id = str(agent_run.get("user_id") or "").strip()
            if not durable_user_id or durable_user_id == "default_user":
                try:
                    await fail_terminal_delivery(
                        "Authenticated dispatch recovery has no durable user identity"
                    )
                except Exception:
                    await release_failed_handoff()
                    raise
                return True
            queued_payload["_sender_user_id"] = durable_user_id
            queued_payload["_sender_display_name"] = durable_user_id
            # Re-resolve the current role after a process restart. The durable
            # outbox intentionally discards the old admin flag, so recovery
            # must not silently downgrade an active admin to a normal user or
            # trust a stale role from the persisted payload.
            try:
                recovered_role = await self._resolve_recovered_user_role(
                    durable_user_id,
                )
            except Exception:
                await release_failed_handoff()
                raise
            if recovered_role is None:
                try:
                    await fail_terminal_delivery(
                        "Authenticated dispatch recovery user is unavailable"
                    )
                except Exception:
                    await release_failed_handoff()
                    raise
                return True
            queued_payload["_sender_is_admin"] = (
                recovered_role == "admin"
            )
        queued_payload["_response_started_at_monotonic"] = time.monotonic()
        lifecycle = {
            "agent_run_id": agent_run_id,
            "terminal": mark_terminal_delivery,
            "terminal_failure": fail_terminal_delivery,
            "cancelled": cancel_terminal_delivery,
            "failure": release_failed_handoff,
            "heartbeat": renew_active_delivery,
            "heartbeat_seconds": 20.0,
            "handed_off": False,
            "explicit_stop": False,
            "settlement": settlement,
        }
        queued_payload["_dispatch_delivery_lifecycle"] = lifecycle
        try:
            accepted = self._queue_user_message(queued_payload)
        except BaseException:
            await release_failed_handoff()
            raise
        if accepted is False:
            await release_failed_handoff()
            return False
        return True

    async def _recover_conversation_dispatches_once(
        self,
        agent_run_service=None,
    ) -> int:
        if not self.on_user_input:
            return 0
        if agent_run_service is None:
            from ...services.agent_run_service import AgentRunService

            service_factory = getattr(
                self,
                "_conversation_dispatch_service_factory",
                AgentRunService,
            )
            agent_run_service = service_factory()
        recovered = 0
        purge_delivered = getattr(
            agent_run_service,
            "purge_delivered_dispatches",
            None,
        )
        if callable(purge_delivered):
            await purge_delivered()
        run_ids = await agent_run_service.list_recoverable_dispatch_run_ids()
        for run_id in run_ids:
            try:
                delivery = await agent_run_service.claim_dispatch_delivery(
                    run_id=run_id,
                    lease_seconds=60.0,
                )
                if delivery is None:
                    continue
                agent_run = await agent_run_service.get_run(run_id)
                if agent_run is None:
                    try:
                        await agent_run_service.mark_dispatch_delivered(
                            run_id=run_id,
                            lease_token=str(delivery["lease_token"]),
                        )
                    except Exception:
                        await agent_run_service.release_dispatch_delivery(
                            run_id=run_id,
                            lease_token=str(delivery["lease_token"]),
                        )
                    continue
                if await self._queue_claimed_dispatch_delivery(
                    agent_run_service,
                    agent_run,
                    delivery,
                ):
                    recovered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to recover conversation dispatch %s", run_id)
        return recovered

    async def _conversation_dispatch_recovery_loop(self) -> None:
        while True:
            try:
                await self._recover_conversation_dispatches_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Conversation dispatch recovery poll failed")
            await asyncio.sleep(5.0)

    async def _start_conversation_dispatch_recovery(self) -> None:
        current = getattr(self, "_conversation_dispatch_recovery_task", None)
        if current and not current.done():
            return
        self._conversation_dispatch_shutting_down = False
        self._conversation_dispatch_recovery_task = asyncio.create_task(
            self._conversation_dispatch_recovery_loop()
        )

    async def _stop_conversation_dispatch_recovery(self) -> None:
        self._conversation_dispatch_shutting_down = True
        recovery_task = getattr(self, "_conversation_dispatch_recovery_task", None)
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        self._conversation_dispatch_recovery_task = None

        tasks = set(self._conversation_dispatch_tasks)
        for generation_tasks in self._conversation_generation_tasks.values():
            tasks.update(generation_tasks)
        late_finalize_tasks = set(
            getattr(self, "_conversation_late_finalize_tasks", set())
        )
        tasks.update(late_finalize_tasks)
        for task in tasks:
            lifecycle = getattr(task, "_dispatch_delivery_lifecycle", None)
            if isinstance(lifecycle, dict):
                lifecycle["explicit_stop"] = False
            if hasattr(task, "done") and not task.done() and hasattr(task, "cancel"):
                task.cancel()
        awaitables = []
        for task in tasks:
            if isinstance(task, (asyncio.Task, asyncio.Future)):
                awaitables.append(task)
            elif isinstance(task, ConcurrentFuture):
                awaitables.append(asyncio.wrap_future(task))
        if awaitables:
            await asyncio.gather(*awaitables, return_exceptions=True)
        late_finalize_registry = getattr(
            self, "_conversation_late_finalize_tasks", None
        )
        if isinstance(late_finalize_registry, set):
            late_finalize_registry.clear()

    def _queue_user_message(self, data: dict):
        """Process a REST-dispatched user message without blocking the client."""
        agent_run_id = str(data.get("agent_run_id") or "").strip()
        active_run_ids = getattr(self, "_conversation_dispatch_run_ids", None)
        if active_run_ids is None:
            active_run_ids = set()
            self._conversation_dispatch_run_ids = active_run_ids
        if agent_run_id and agent_run_id in active_run_ids:
            return False
        if agent_run_id:
            active_run_ids.add(agent_run_id)
        try:
            task = asyncio.create_task(self._handle_user_message_background(data))
        except Exception:
            if agent_run_id:
                active_run_ids.discard(agent_run_id)
            raise
        self._conversation_dispatch_tasks.add(task)
        lifecycle = data.get("_dispatch_delivery_lifecycle")
        if isinstance(lifecycle, dict):
            setattr(task, "_dispatch_delivery_lifecycle", lifecycle)

        def cleanup(completed_task):
            self._conversation_dispatch_tasks.discard(completed_task)
            if agent_run_id:
                active_run_ids.discard(agent_run_id)

        task.add_done_callback(cleanup)
        return True

    async def _attach_project_to_conversation_if_missing(
        self,
        session_id: Optional[str],
        project_id: Optional[str],
        *,
        user_id: Optional[str] = None,
        user_role: Optional[str] = None,
        authenticated: bool = False,
        trusted_legacy: bool = False,
    ) -> Optional[str]:
        """Return the effective Project id for a turn and attach it if needed.

        WebSocket clients normally send ``project_id`` on every turn, but a
        session already scoped to a Project must remain discoverable when an
        older client omits it (notably for attachment turns).
        """
        server_auth_enabled = getattr(self, "auth_enabled", None)
        identity_present = bool(user_id and user_id != "default_user")
        legacy_allowed = trusted_legacy and server_auth_enabled is False
        auth_scope_required = authenticated or server_auth_enabled is True

        if not session_id:
            if not project_id:
                return None
            if not identity_present:
                if legacy_allowed:
                    return str(project_id)
                if auth_scope_required:
                    raise PermissionError("Authenticated user identity is required")
                return None
            if not user_id or user_id == "default_user":
                raise PermissionError("Authenticated user identity is required")
            try:
                parsed_project_id = UUID(str(project_id))
            except (TypeError, ValueError):
                raise ValueError("Invalid project id") from None

            await self._assert_project_read_access_for_turn(
                parsed_project_id,
                user_id=user_id,
                user_role=user_role,
            )
            return str(parsed_project_id)
        parsed_project_id = None
        if project_id:
            try:
                parsed_project_id = UUID(str(project_id))
            except (TypeError, ValueError):
                raise ValueError("Invalid project id") from None

        try:
            from ...memory.conversation_repository import ConversationRepository

            repository = ConversationRepository()
            conversation = await repository.get_session_by_id(
                session_id, with_messages=False
            )
            existing_project_id = (
                UUID(str(conversation.project_id))
                if conversation and conversation.project_id
                else None
            )
            if (
                parsed_project_id is not None
                and existing_project_id is not None
                and parsed_project_id != existing_project_id
            ):
                raise ValueError("Project does not match conversation")

            effective_project_id = parsed_project_id or existing_project_id
            if effective_project_id and not identity_present and not legacy_allowed:
                raise PermissionError("Authenticated user identity is required")
            if effective_project_id and server_auth_enabled is True and not authenticated:
                raise PermissionError("Authenticated user identity is required")
            if effective_project_id and user_id and user_id != "default_user":
                await self._assert_project_read_access_for_turn(
                    effective_project_id,
                    user_id=user_id,
                    user_role=user_role,
                )

            if conversation and parsed_project_id and existing_project_id is None:
                attached = await self._atomically_attach_project_to_conversation(
                    session_id,
                    parsed_project_id,
                )
                if not attached:
                    current = await repository.get_session_by_id(
                        session_id, with_messages=False
                    )
                    current_project_id = (
                        UUID(str(current.project_id))
                        if current and current.project_id
                        else None
                    )
                    if current_project_id != parsed_project_id:
                        raise ValueError("Project does not match conversation")
            return str(effective_project_id) if effective_project_id else None
        except (PermissionError, ValueError):
            raise
        except Exception:
            logger.exception(
                "Failed to attach project %s to conversation %s",
                project_id,
                session_id,
            )
            # A repository/database failure is transient.  Preserve the
            # original exception so queued dispatch can release its lease for
            # retry instead of terminalizing an authorization decision that
            # was never actually made.
            raise

    async def _assert_project_read_access_for_turn(
        self,
        project_id: UUID,
        *,
        user_id: str,
        user_role: Optional[str],
    ) -> None:
        from ...memory.database import get_database_manager
        from ...memory.project_repository import ProjectRepository
        from ...services.project_context import has_project_read_access

        db_session = None
        try:
            db_session = await get_database_manager().get_session()
            project = await ProjectRepository.get_by_id(db_session, project_id)
            if project is None or not await has_project_read_access(
                db_session,
                project,
                user_id=user_id,
                user_role=user_role,
            ):
                raise PermissionError("Project access denied")
        except PermissionError:
            raise
        except Exception as exc:
            logger.exception("Project access check failed for %s", project_id)
            # Do not turn an unavailable authorization dependency into a
            # durable access denial.  The dispatch worker classifies this as
            # transient and will retry after the database recovers.
            raise
        finally:
            if db_session is not None:
                await db_session.close()

    async def _assert_project_write_access_for_turn(
        self,
        project_id: UUID,
        *,
        user_id: str,
    ) -> None:
        """Require the durable Project write grant for a generated turn.

        Conversation membership is intentionally not enough here: a member can
        retain conversation visibility after their Project write grant is
        revoked. Generation persists messages and may mutate the session, so
        the backend re-checks the current Project ACL immediately before the
        generation path.
        """
        from uuid import UUID as UUIDType

        from ...memory.database import get_database_manager
        from ...memory.project_repository import ProjectRepository

        try:
            caller_id = UUIDType(str(user_id))
        except (TypeError, ValueError):
            raise PermissionError("Authenticated user identity is required") from None

        db_session = None
        try:
            db_session = await get_database_manager().get_session()
            allowed = await ProjectRepository.has_permission(
                db_session,
                project_id,
                caller_id,
                "write",
            )
            if not allowed:
                raise PermissionError("Project write access denied")
        except PermissionError:
            raise
        except Exception:
            logger.exception("Project write access check failed for %s", project_id)
            # Database failures are deliberately propagated so queued dispatch
            # can release its lease and retry instead of terminalizing access.
            raise
        finally:
            if db_session is not None:
                await db_session.close()

    async def _atomically_attach_project_to_conversation(
        self,
        session_id: str,
        project_id: UUID,
    ) -> bool:
        """Attach a project only while the conversation is still unscoped."""
        from sqlalchemy import and_, update

        from ...memory.database import get_database_manager
        from ...memory.models import ConversationSession

        db_session = None
        try:
            db_session = await get_database_manager().get_session()
            result = await db_session.execute(
                update(ConversationSession)
                .where(
                    and_(
                        ConversationSession.id == UUID(str(session_id)),
                        ConversationSession.deleted_at.is_(None),
                        ConversationSession.project_id.is_(None),
                    )
                )
                .values(project_id=project_id)
            )
            await db_session.commit()
            return bool(getattr(result, "rowcount", 0))
        except Exception:
            if db_session is not None:
                try:
                    await db_session.rollback()
                except Exception:
                    logger.exception("Failed to roll back project attach")
            raise
        finally:
            if db_session is not None:
                await db_session.close()

    async def _attach_app_to_conversation_if_missing(
        self,
        session_id: Optional[str],
        app_id: Optional[str],
        app_target_id: Optional[str],
        *,
        user_id: str,
        project_id: Optional[str],
        user_role: Optional[str] = None,
        app_context_provided: bool = False,
    ) -> None:
        """Persist one validated App context on a conversation session.

        The websocket path can receive a context before the durable dispatch
        endpoint, so it must perform the same server-side UUID/permission check
        and must never trust the client's display name.
        """
        if not session_id or (not app_id and not app_context_provided):
            return
        from ...memory.conversation_repository import ConversationRepository

        repository = ConversationRepository()
        conversation = await repository.get_session_by_id(session_id, with_messages=False)
        if conversation is None:
            raise ValueError("Conversation session not found")
        if not app_id:
            if app_target_id:
                raise ValueError("app_target_id には app_id が必要です")
            await repository.update_session(
                session_id,
                touch_activity=False,
                app_id=None,
                app_target_id=None,
            )
            return
        from uuid import UUID

        from sqlalchemy import and_, select

        from ...memory.database import get_database_manager
        from ...memory.models import App, AppTarget, ProjectApp
        from ...services.app_service import AppAccessError, AppService

        try:
            app_uuid = UUID(str(app_id))
            target_uuid = UUID(str(app_target_id)) if app_target_id else None
            user_uuid = UUID(str(user_id))
            project_uuid = UUID(str(project_id)) if project_id else None
        except (TypeError, ValueError) as exc:
            raise ValueError("App context UUIDが不正です") from exc

        db_session = await get_database_manager().get_session()
        try:
            app = await db_session.scalar(select(App).where(App.id == app_uuid).limit(1))
            if not app:
                raise ValueError("App not found")
            try:
                await AppService().require_permission(
                    db_session,
                    app,
                    user_id=user_uuid,
                    required="viewer",
                    user_role=user_role,
                    project_id=project_uuid,
                )
            except AppAccessError as exc:
                raise PermissionError("Appを閲覧できません") from exc
            if project_uuid is not None:
                binding = await db_session.scalar(select(ProjectApp).where(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app_uuid,
                ).limit(1))
                if binding is None or not binding.enabled:
                    raise PermissionError("このProjectではAppが有効化されていません")
            if target_uuid:
                target = await db_session.scalar(select(AppTarget).where(
                    and_(AppTarget.id == target_uuid, AppTarget.app_id == app_uuid)
                ).limit(1))
                if not target:
                    raise ValueError("App Target not found")
        finally:
            await db_session.close()
        await repository.update_session(
            session_id,
            touch_activity=False,
            app_id=app_uuid,
            app_target_id=target_uuid,
        )

    async def _handle_user_message_background(self, data: dict):
        lifecycle = data.get("_dispatch_delivery_lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else None
        try:
            await self._set_dispatch_user_context(data)
            await self._handle_user_message(data)
        except BaseException as exc:
            if lifecycle and not lifecycle.get("handed_off"):
                failure_name = (
                    "terminal_failure"
                    if isinstance(exc, (PermissionError, ValueError))
                    else "failure"
                )
                failure = lifecycle.get(failure_name)
                if callable(failure):
                    try:
                        settled = (
                            await failure(str(exc))
                            if failure_name == "terminal_failure"
                            else await failure()
                        )
                        if settled is False and failure_name == "terminal_failure":
                            fallback = lifecycle.get("failure")
                            if callable(fallback):
                                await fallback()
                    except Exception:
                        logger.exception("Failed to settle pre-handoff dispatch delivery")
                        if failure_name == "terminal_failure":
                            fallback = lifecycle.get("failure")
                            if callable(fallback):
                                try:
                                    await fallback()
                                except Exception:
                                    logger.exception(
                                        "Failed to release dispatch delivery lease"
                                    )
            logger.exception("Failed to process queued conversation message")
            if isinstance(exc, asyncio.CancelledError):
                raise
        else:
            if lifecycle and not lifecycle.get("handed_off"):
                failure = lifecycle.get("failure")
                if callable(failure):
                    try:
                        await failure()
                    except Exception:
                        logger.exception("Failed to release unused delivery lease")
        finally:
            # ContextVars are task-local, but clearing here also protects a
            # long-lived worker task if a callback is rejected before handoff.
            from ...tools.os_operations.tools import clear_user_context

            clear_user_context()

    def _conversation_control_key(self, session_id: Optional[str]) -> str:
        session_key = str(session_id or "").strip()
        return session_key or "__default__"

    def _generation_status_key(self, session_id: Optional[str]) -> str:
        return self._conversation_control_key(session_id)

    def _ensure_generation_status_store(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_conversation_generation_status"):
            self._conversation_generation_status = {}
        return self._conversation_generation_status

    def _fence_generation_run(
        self, session_id: Optional[str], agent_run_id: Optional[str]
    ) -> None:
        run_id = str(agent_run_id or "").strip()
        if not run_id:
            return
        key = self._generation_status_key(session_id)
        fenced = getattr(self, "_fenced_generation_run_ids", None)
        if fenced is None:
            fenced = {}
            self._fenced_generation_run_ids = fenced
        fenced.setdefault(key, set()).add(run_id)

    def _is_fenced_generation_run(
        self, session_id: Optional[str], agent_run_id: Optional[str]
    ) -> bool:
        run_id = str(agent_run_id or "").strip()
        if not run_id:
            return False
        fenced = getattr(self, "_fenced_generation_run_ids", {})
        return run_id in fenced.get(self._generation_status_key(session_id), set())

    def _generation_persistence_lock(
        self, session_id: Optional[str], agent_run_id: Optional[str]
    ) -> Optional[asyncio.Lock]:
        """Return the gate shared by run-scoped persistence and stop fencing."""
        run_id = str(agent_run_id or "").strip()
        if not run_id:
            return None
        locks = getattr(self, "_generation_persistence_locks", None)
        if locks is None:
            locks = {}
            self._generation_persistence_locks = locks
        key = f"{self._generation_status_key(session_id)}|{run_id}"
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    async def _fence_generation_run_async(
        self, session_id: Optional[str], agent_run_id: Optional[str]
    ) -> None:
        lock = self._generation_persistence_lock(session_id, agent_run_id)
        if lock is None:
            self._fence_generation_run(session_id, agent_run_id)
            return
        async with lock:
            self._fence_generation_run(session_id, agent_run_id)

    def _now_iso(self) -> str:
        return f"{datetime.utcnow().isoformat(timespec='milliseconds')}Z"

    def _extract_generation_status_message(self, data: Dict[str, Any]) -> Optional[str]:
        nested = data.get("data")
        nested_data = nested if isinstance(nested, dict) else {}
        for key in ("message", "content", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            nested_value = nested_data.get(key)
            if isinstance(nested_value, str) and nested_value.strip():
                return nested_value.strip()
        return None

    def _set_conversation_generation_status(
        self,
        session_id: Optional[str],
        *,
        running: bool,
        status: str,
        message: Optional[str] = None,
        active_tool: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = self._generation_status_key(session_id)
        store = self._ensure_generation_status_store()
        previous = store.get(key, {})
        now = self._now_iso()
        previous_run_id = str(previous.get("agent_run_id") or "").strip()
        next_run_id = str(agent_run_id or "").strip()
        identity_rotated = status == "queued" or bool(
            next_run_id and previous_run_id and next_run_id != previous_run_id
        )
        payload = {
            "session_id": session_id,
            "running": running,
            "status": status,
            "message": message,
            "active_tool": active_tool,
            "agent_run_id": (
                agent_run_id
                if agent_run_id is not None or identity_rotated
                else previous.get("agent_run_id")
            ),
            "client_message_id": (
                client_message_id
                if client_message_id is not None or identity_rotated
                else previous.get("client_message_id")
            ),
            "started_at": (
                now
                if identity_rotated
                else (previous.get("started_at") if previous else now)
            ),
            "updated_at": now,
        }
        store[key] = payload
        return payload

    def get_conversation_generation_status(
        self, session_id: Optional[str]
    ) -> Dict[str, Any]:
        key = self._generation_status_key(session_id)
        status = self._ensure_generation_status_store().get(key)
        tasks = self._conversation_generation_tasks.get(key, set())
        running = any(not task.done() for task in tasks if hasattr(task, "done"))
        if status:
            return dict(status)
        return {
            "session_id": session_id,
            "running": running,
            "status": "running" if running else "idle",
            "message": "応答を生成しています" if running else None,
            "active_tool": None,
            "agent_run_id": None,
            "client_message_id": None,
            "started_at": None,
            "updated_at": None,
        }

    def _update_generation_status_from_stream_event(
        self, event_type: str, data: Dict[str, Any]
    ) -> None:
        nested = data.get("data")
        nested_data = nested if isinstance(nested, dict) else {}
        session_id = data.get("session_id", nested_data.get("session_id"))
        if not session_id:
            return

        message = self._extract_generation_status_message(data)
        tool = data.get("tool", nested_data.get("tool"))
        active_tool = str(tool) if isinstance(tool, str) and tool else None
        raw_agent_run_id = data.get("agent_run_id", nested_data.get("agent_run_id"))
        agent_run_id = str(raw_agent_run_id) if raw_agent_run_id else None
        raw_client_message_id = data.get(
            "client_message_id", nested_data.get("client_message_id")
        )
        client_message_id = (
            str(raw_client_message_id) if raw_client_message_id else None
        )
        current_status = self._ensure_generation_status_store().get(
            self._generation_status_key(session_id), {}
        )
        current_run_id = str(current_status.get("agent_run_id") or "").strip()
        # A late event from an older run must not overwrite the status of a
        # newer run in the same conversation.  A new stream_start is the one
        # event allowed to rotate the status to its newly-created run.
        if (
            agent_run_id
            and current_run_id
            and agent_run_id != current_run_id
            and event_type != "stream_start"
        ):
            return

        if event_type == "stream_start":
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status=str(data.get("status") or "running"),
                message=message or "応答を生成しています",
                active_tool=None,
                agent_run_id=agent_run_id,
                client_message_id=client_message_id,
            )
        elif event_type == "tool_start":
            tool_message = message or (
                f"{active_tool} を実行しています"
                if active_tool
                else "ツールを実行しています"
            )
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="tool",
                message=tool_message,
                active_tool=active_tool,
                agent_run_id=agent_run_id,
            )
        elif event_type == "tool_end":
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="running",
                message=message or "ツール実行が完了しました",
                active_tool=None,
                agent_run_id=agent_run_id,
                client_message_id=client_message_id,
            )
        elif (
            event_type == "steering_update"
            and str(data.get("status") or "") == "rejected"
        ):
            # A rejected Ctrl+Enter is control feedback, not a new generation.
            # Preserve the existing terminal/idle state while still broadcasting
            # the event so the optimistic user message can show the failure.
            return
        elif event_type in {"status_update", "reasoning_progress", "steering_update"}:
            previous = self._ensure_generation_status_store().get(
                self._generation_status_key(session_id), {}
            )
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status=str(data.get("status") or event_type),
                message=message or previous.get("message") or "応答を生成しています",
                active_tool=previous.get("active_tool"),
                agent_run_id=agent_run_id,
            )
        elif event_type in {"stream_end", "response"}:
            failed = (
                str(data.get("status") or nested_data.get("status") or "").lower()
                == "failed"
            )
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="failed" if failed else "completed",
                # 失敗時は emitter が渡す分類済み文言を優先する。
                # 分類済み文言が無い場合だけ、原因を誤誘導しない既定文言を使う。
                message=message
                or (
                    DEFAULT_GENERATION_FAILURE_MESSAGE
                    if failed
                    else "応答生成が完了しました"
                ),
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "stream_cancelled":
            cancellation_status = str(data.get("status") or "cancelled")
            cancellation_pending = cancellation_status == "cancellation_pending"
            cancellation_failed = cancellation_status == "cancellation_failed"
            self._set_conversation_generation_status(
                session_id,
                running=cancellation_pending or cancellation_failed,
                status=(
                    "cancellation_pending"
                    if cancellation_pending
                    else (
                        "cancellation_failed"
                        if cancellation_failed
                        else "cancelled"
                    )
                ),
                message=message or "応答生成を停止しました",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "conversation_persisted" and data.get("role") == "assistant":
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="completed",
                message="応答を保存しました",
                active_tool=None,
                agent_run_id=agent_run_id,
            )

    def _register_conversation_generation_task(
        self, session_id: Optional[str], task: Any
    ) -> None:
        key = self._conversation_control_key(session_id)
        tasks = self._conversation_generation_tasks.setdefault(key, set())
        tasks.add(task)

        def _discard(done_task: Any) -> None:
            current_tasks = self._conversation_generation_tasks.get(key)
            no_current_tasks = False
            if current_tasks is not None:
                current_tasks.discard(done_task)
                if not current_tasks:
                    self._conversation_generation_tasks.pop(key, None)
                    no_current_tasks = True
            try:
                done_task.result()
            except (asyncio.CancelledError, FutureCancelledError):
                logger.info("Conversation generation cancelled: %s", key)
                if no_current_tasks:
                    self._set_conversation_generation_status(
                        session_id,
                        running=False,
                        status="cancelled",
                        message="応答生成を停止しました",
                        active_tool=None,
                    )
            except Exception:
                logger.exception("Conversation generation failed: %s", key)
                if no_current_tasks:
                    self._set_conversation_generation_status(
                        session_id,
                        running=False,
                        status="failed",
                        message="応答生成中にエラーが発生しました",
                        active_tool=None,
                    )
            else:
                if no_current_tasks:
                    current_status = self.get_conversation_generation_status(session_id)
                    if current_status.get("running"):
                        self._set_conversation_generation_status(
                            session_id,
                            running=False,
                            status="completed",
                            message="応答生成が完了しました",
                            active_tool=None,
                        )

        if hasattr(task, "add_done_callback"):
            task.add_done_callback(_discard)

    def _schedule_user_input_callback(
        self,
        *,
        message: str,
        persist_content: Optional[str] = None,
        image_data: Optional[dict],
        session_id: Optional[str],
        project_id: Optional[str],
        generation_profile: Optional[str],
        planning_policy: Optional[str] = None,
        include_project_context: bool,
        edit_message_id: Optional[str],
        response_model: Optional[Dict[str, str]],
        client_message_id: Optional[str],
        attachments: List[Dict[str, Any]],
        attachment_context: Optional[str],
        media_recognition_metadata: Optional[List[Dict[str, Any]]] = None,
        skip_user_persistence: bool = False,
        persisted_user_message_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        assistant_sender_type: Optional[str] = None,
        assistant_sender_id: Optional[str] = None,
        assistant_sender_display_name: Optional[str] = None,
        sender_user_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
        response_started_at_monotonic: Optional[float] = None,
        command_capabilities: Optional[List[str]] = None,
        tools_required: Optional[bool] = None,
        dispatch_lifecycle: Optional[Dict[str, Any]] = None,
        docs_reference_ids: Optional[Iterable[str]] = None,
        task_id: Optional[str] = None,
        explicit_references: Optional[Iterable[Any]] = None,
        verified_project_attachment: bool = False,
    ) -> None:
        if session_id:
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="queued",
                message="応答生成を開始しています",
                active_tool=None,
                agent_run_id=agent_run_id,
                client_message_id=client_message_id,
            )

        def create_user_input_coro():
            callback_kwargs = {
                "persist_content": persist_content,
                "image_data": image_data,
                "session_id": session_id,
                "project_id": project_id,
                "generation_profile": generation_profile,
                "planning_policy": planning_policy,
                "include_project_context": include_project_context,
                "edit_message_id": edit_message_id,
                "response_model": response_model,
                "client_message_id": client_message_id,
                "attachments": attachments,
                "attachment_context": attachment_context,
                "media_recognition_metadata": media_recognition_metadata,
                "skip_user_persistence": skip_user_persistence,
                "persisted_user_message_id": persisted_user_message_id,
                "agent_run_id": agent_run_id,
                "assistant_sender_type": assistant_sender_type,
                "assistant_sender_id": assistant_sender_id,
                "assistant_sender_display_name": assistant_sender_display_name,
                "sender_user_id": sender_user_id,
                "sender_display_name": sender_display_name,
                "response_started_at_monotonic": response_started_at_monotonic,
                "command_capabilities": command_capabilities,
                "tools_required": tools_required,
            }
            normalized_docs_reference_ids = tuple(
                str(value).strip().lower()
                for value in docs_reference_ids or ()
                if str(value).strip()
            )
            if normalized_docs_reference_ids:
                callback_kwargs["docs_reference_ids"] = normalized_docs_reference_ids
            if verified_project_attachment:
                callback_kwargs["verified_project_attachment"] = True
            return self.on_user_input(message, **callback_kwargs)

        cancellation_handle = register_generation_cancellation(agent_run_id)

        async def cancellation_scoped_callback():
            permission_scope_token = set_permission_session_key(
                f"{sender_user_id or 'default_user'}|{session_id or 'default'}"
            )
            callback_turn_context_token = None
            if task_id or explicit_references:
                current_turn = get_turn_context()
                callback_turn_context_token = set_turn_context(
                    user_id=current_turn.user_id or sender_user_id,
                    project_id=current_turn.project_id or project_id,
                    include_project_context=current_turn.include_project_context,
                    session_id=current_turn.session_id or session_id,
                    task_id=task_id or current_turn.task_id,
                    message_id=current_turn.message_id,
                    client_message_id=current_turn.client_message_id,
                    tool_call_id=current_turn.tool_call_id,
                    docs_reference_ids=current_turn.docs_reference_ids,
                    explicit_references=(
                        tuple(explicit_references)
                        if explicit_references
                        else current_turn.explicit_references
                    ),
                    verified_project_attachment=current_turn.verified_project_attachment,
                )
            # Give an immediately-following stop request a chance to cancel the
            # dispatch before user input enters the generation pipeline.  This
            # also makes the hand-off deterministic when the callback and the
            # control message are scheduled in the same event-loop turn.
            try:
                await asyncio.sleep(0)
                token = set_current_generation_cancellation(cancellation_handle)
                try:
                    return await create_user_input_coro()
                finally:
                    reset_current_generation_cancellation(token)
                    if cancellation_handle is not None:
                        if not cancellation_handle.worker_started.is_set():
                            cancellation_handle.worker_completed.set()
                        release_generation_cancellation(cancellation_handle)
            finally:
                if callback_turn_context_token is not None:
                    reset_turn_context(callback_turn_context_token)
                reset_permission_session_key(permission_scope_token)

        if dispatch_lifecycle:
            dispatch_lifecycle["handed_off"] = True
            heartbeat = dispatch_lifecycle.get("heartbeat")
            heartbeat_seconds = float(
                dispatch_lifecycle.get("heartbeat_seconds") or 20.0
            )
            settlement_attempt = dispatch_lifecycle.setdefault(
                "_settlement_attempt",
                {"made": False},
            )

            async def settle(name: str) -> None:
                if settlement_attempt["made"]:
                    return
                settlement_attempt["made"] = True
                callback = dispatch_lifecycle.get(name)
                if not callable(callback):
                    return
                settled = await callback()
                if settled is False:
                    raise RuntimeError(f"dispatch lifecycle settlement failed: {name}")

            async def tracked_callback():
                callback_task = asyncio.create_task(cancellation_scoped_callback())
                try:
                    while True:
                        completed, _pending = await asyncio.wait(
                            {callback_task},
                            timeout=max(1.0, heartbeat_seconds),
                        )
                        if completed:
                            result = await callback_task
                            await settle("terminal")
                            return result
                        if callable(heartbeat) and not await heartbeat():
                            callback_task.cancel()
                            await asyncio.gather(
                                callback_task,
                                return_exceptions=True,
                            )
                            await settle("failure")
                            raise RuntimeError("durable dispatch lease was lost")
                except asyncio.CancelledError:
                    if not callback_task.done():
                        callback_task.cancel()
                        await asyncio.gather(
                            callback_task,
                            return_exceptions=True,
                        )
                    if dispatch_lifecycle.get("explicit_stop"):
                        await settle("cancelled")
                    else:
                        await settle("failure")
                    raise
                except BaseException:
                    if callback_task.done() and not callback_task.cancelled():
                        await settle("terminal")
                    else:
                        if not callback_task.done():
                            callback_task.cancel()
                            await asyncio.gather(
                                callback_task,
                                return_exceptions=True,
                            )
                        await settle("failure")
                    raise
                finally:
                    if (
                        cancellation_handle is not None
                        and not cancellation_handle.worker_started.is_set()
                        and callback_task.cancelled()
                    ):
                        callback_task.get_coro().close()
                    if not settlement_attempt["made"]:
                        if dispatch_lifecycle.get("explicit_stop"):
                            await settle("cancelled")
                        else:
                            await settle("failure")

            callback_coro = tracked_callback()
        else:
            callback_coro = cancellation_scoped_callback()
        if self.main_event_loop:
            future = asyncio.run_coroutine_threadsafe(
                callback_coro,
                self.main_event_loop,
            )
            setattr(future, "_generation_callback_coro", callback_coro)
            setattr(future, "_dispatch_delivery_lifecycle", dispatch_lifecycle)
            setattr(future, "_agent_run_id", agent_run_id)
            self._register_conversation_generation_task(session_id, future)
        else:
            task = asyncio.create_task(callback_coro)
            setattr(task, "_generation_callback_coro", callback_coro)
            if dispatch_lifecycle:
                setattr(task, "_dispatch_delivery_lifecycle", dispatch_lifecycle)
            setattr(task, "_agent_run_id", agent_run_id)
            self._register_conversation_generation_task(session_id, task)

    async def _handle_stop_generation(self, data: dict) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        requested_run_id = str(
            data.get("agent_run_id") or ""
        ).strip() if isinstance(data, dict) else ""
        key = self._conversation_control_key(session_id)
        all_tasks = list(self._conversation_generation_tasks.get(key, set()))
        tasks = [
            task
            for task in all_tasks
            if not requested_run_id
            or str(getattr(task, "_agent_run_id", "") or "").strip()
            == requested_run_id
        ]
        cancelled = 0
        lifecycle_managed_run_ids: set[str] = set()
        run_ids: list[str] = []
        cancelled_awaitables = []
        cancellation_handles: dict[str, Any] = {}
        lifecycle_settlements: list[tuple[str, Dict[str, Any]]] = []
        current_status = self.get_conversation_generation_status(session_id)
        status_run_id = str(current_status.get("agent_run_id") or "").strip()
        if status_run_id and (
            not requested_run_id or status_run_id == requested_run_id
        ):
            run_ids.append(status_run_id)
            await self._fence_generation_run_async(session_id, status_run_id)
            handle = request_generation_cancellation(status_run_id)
            if handle is not None:
                cancellation_handles[status_run_id] = handle
        human_interaction_manager = getattr(self, "_human_interaction_manager", None)
        if human_interaction_manager is not None:
            for run_id in {rid for rid in run_ids if rid}:
                human_interaction_manager.terminalize_run(run_id)
            if status_run_id:
                human_interaction_manager.terminalize_run(status_run_id)
        for task in tasks:
            if hasattr(task, "done") and task.done():
                continue
            task_run_id = str(getattr(task, "_agent_run_id", "") or "").strip()
            if task_run_id:
                run_ids.append(task_run_id)
                await self._fence_generation_run_async(session_id, task_run_id)
            lifecycle = getattr(task, "_dispatch_delivery_lifecycle", None)
            if isinstance(lifecycle, dict):
                lifecycle["explicit_stop"] = True
                lifecycle_run_id = str(
                    lifecycle.get("agent_run_id") or task_run_id
                ).strip()
                if lifecycle_run_id:
                    lifecycle_managed_run_ids.add(lifecycle_run_id)
                    lifecycle_settlements.append((lifecycle_run_id, lifecycle))
            if task_run_id:
                handle = request_generation_cancellation(task_run_id)
                if handle is not None:
                    cancellation_handles[task_run_id] = handle
            if hasattr(task, "cancel"):
                task.cancel()
                cancelled += 1
                if isinstance(task, (asyncio.Task, asyncio.Future)):
                    cancelled_awaitables.append(task)
                elif isinstance(task, ConcurrentFuture):
                    cancelled_awaitables.append(asyncio.wrap_future(task))

        # Resolve a requested run from both the status record and task
        # metadata.  The status can already point at a newer run while an
        # older task is still draining, so checking only status before the
        # task scan incorrectly returned not_found for the older run.
        if requested_run_id and requested_run_id not in run_ids:
            return {
                "session_id": session_id,
                "cancelled": 0,
                "agent_run_id": requested_run_id,
                "agent_run_ids": [],
                "status": "not_found",
            }

        if cancelled_awaitables:
            await asyncio.gather(*cancelled_awaitables, return_exceptions=True)

        for task in tasks:
            if not isinstance(task, asyncio.Task):
                continue
            task_run_id = str(getattr(task, "_agent_run_id", "") or "").strip()
            handle = cancellation_handles.get(task_run_id)
            if handle is None or handle.worker_started.is_set():
                continue
            callback_coro = getattr(task, "_generation_callback_coro", None)
            if callback_coro is not None and hasattr(callback_coro, "close"):
                callback_coro.close()

        lifecycle_settlement_failed_run_ids: list[str] = []
        settled_lifecycle_ids: set[int] = set()
        for lifecycle_run_id, lifecycle in lifecycle_settlements:
            lifecycle_id = id(lifecycle)
            if lifecycle_id in settled_lifecycle_ids:
                continue
            settled_lifecycle_ids.add(lifecycle_id)
            settlement_attempt = lifecycle.get("_settlement_attempt")
            if not isinstance(settlement_attempt, dict):
                settlement_attempt = {"made": False}
                lifecycle["_settlement_attempt"] = settlement_attempt
            if settlement_attempt.get("made"):
                continue
            settlement_attempt["made"] = True
            callback = lifecycle.get("cancelled")
            try:
                if callable(callback) and await callback() is False:
                    raise RuntimeError(
                        "dispatch lifecycle settlement failed: cancelled"
                    )
            except Exception:
                lifecycle_settlement_failed_run_ids.append(lifecycle_run_id)
                logger.exception(
                    "Failed to settle cancelled dispatch lifecycle: %s",
                    lifecycle_run_id,
                )

        for handle in cancellation_handles.values():
            if not handle.worker_started.is_set():
                handle.worker_completed.set()
                release_generation_cancellation(handle)

        self._conversation_steering_queues.pop(key, None)
        distinct_run_ids = list(dict.fromkeys(run_ids))
        finalized_messages: list[dict[str, Any]] = []
        persistence_failed_run_ids: list[str] = list(
            lifecycle_settlement_failed_run_ids
        )
        drain_timed_out_run_ids: list[str] = []

        async def wait_for_worker_drain(run_id: str, handle: Any):
            drain_timeout = float(
                getattr(
                    self,
                    "_conversation_cancel_drain_timeout_seconds",
                    15.0,
                )
            )
            deadline = asyncio.get_running_loop().time() + max(0.0, drain_timeout)
            while not handle.worker_completed.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))
            drained = handle.worker_completed.is_set()
            return run_id, drained

        drain_results = await asyncio.gather(
            *(
                wait_for_worker_drain(run_id, handle)
                for run_id, handle in cancellation_handles.items()
            )
        )
        for run_id, drained in drain_results:
            if not drained:
                persistence_failed_run_ids.append(run_id)
                drain_timed_out_run_ids.append(run_id)
                logger.error(
                    "Timed out draining cancelled generation worker: %s",
                    run_id,
                )
        if distinct_run_ids:
            try:
                from ...services.agent_run_service import AgentRunService

                service_factory = getattr(
                    self,
                    "_conversation_agent_run_service_factory",
                    AgentRunService,
                )
                agent_run_service = service_factory()
                for run_id in distinct_run_ids:
                    if run_id in persistence_failed_run_ids:
                        continue
                    try:
                        if run_id not in lifecycle_managed_run_ids:
                            await agent_run_service.cancel_run(run_id)
                        finalized_message = (
                            await agent_run_service.finalize_cancelled_chat_turn(run_id)
                        )
                        if finalized_message:
                            finalized_messages.append(finalized_message)
                    except Exception:
                        persistence_failed_run_ids.append(run_id)
                        logger.exception(
                            "Failed to finalize cancelled agent run: %s",
                            run_id,
                        )
            except Exception:
                persistence_failed_run_ids.extend(
                    run_id
                    for run_id in distinct_run_ids
                    if run_id not in persistence_failed_run_ids
                )
                logger.exception(
                    "Failed to finalize cancelled agent runs: %s",
                    distinct_run_ids,
                )
        persistence_failed_run_ids = sorted(set(persistence_failed_run_ids))
        drain_timed_out_run_ids = sorted(set(drain_timed_out_run_ids))

        async def finalize_after_late_drain(run_id: str, handle: Any) -> None:
            late_drain_timeout = float(
                getattr(
                    self,
                    "_conversation_cancel_late_drain_timeout_seconds",
                    300.0,
                )
            )
            deadline = asyncio.get_running_loop().time() + max(
                0.0, late_drain_timeout
            )
            while not handle.worker_completed.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))
            drained = handle.worker_completed.is_set()
            if not drained:
                logger.error(
                    "Cancelled generation worker never drained: %s",
                    run_id,
                )
                # The worker is no longer allowed to affect a later turn.  A
                # cancellation_failed event is terminal only because every
                # subsequent event carrying this run id is fenced below.
                self._fence_generation_run(session_id, run_id)
                await self.broadcast_stream_event(
                    "stream_cancelled",
                    {
                        "session_id": session_id,
                        "agent_run_id": run_id,
                        "status": "cancellation_failed",
                        "message": "応答生成を完全に停止できませんでした",
                        "cancelled": 0,
                        "persistence_failed": True,
                        "persistence_failed_run_ids": [run_id],
                    },
                )
                return
            try:
                from ...services.agent_run_service import AgentRunService

                service_factory = getattr(
                    self,
                    "_conversation_agent_run_service_factory",
                    AgentRunService,
                )
                service = service_factory()
                if run_id not in lifecycle_managed_run_ids:
                    await service.cancel_run(run_id)
                message_payload = await service.finalize_cancelled_chat_turn(run_id)
                retry_event = {
                    "session_id": session_id,
                    "agent_run_id": run_id,
                    "status": "cancelled",
                    "message": "停止済み応答を保存しました",
                    "cancelled": 0,
                    "persistence_failed": False,
                }
                if message_payload:
                    retry_event.update(
                        {
                            "message_id": message_payload.get("id"),
                            "content": message_payload.get("content", ""),
                            "metadata": message_payload.get("metadata", {}),
                            "messages": [message_payload],
                        }
                    )
                await self.broadcast_stream_event(
                    "stream_cancelled",
                    retry_event,
                )
            except Exception:
                logger.exception(
                    "Failed to finalize late-drained generation: %s",
                    run_id,
                )
                self._set_conversation_generation_status(
                    session_id,
                    running=False,
                    status="cancelled",
                    message="停止しましたが、途中の応答を保存できませんでした",
                    active_tool=None,
                    agent_run_id=run_id,
                )
                try:
                    await self.broadcast_stream_event(
                        "stream_cancelled",
                        {
                            "session_id": session_id,
                            "agent_run_id": run_id,
                            "status": "cancelled",
                            "message": (
                                "停止しましたが、途中の応答を保存できませんでした"
                            ),
                            "cancelled": 0,
                            "persistence_failed": True,
                            "persistence_failed_run_ids": [run_id],
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to broadcast late persistence failure: %s",
                        run_id,
                    )

        if drain_timed_out_run_ids:
            late_finalize_tasks = getattr(
                self,
                "_conversation_late_finalize_tasks",
                None,
            )
            if late_finalize_tasks is None:
                late_finalize_tasks = set()
                self._conversation_late_finalize_tasks = late_finalize_tasks
            for run_id in drain_timed_out_run_ids:
                handle = cancellation_handles.get(run_id)
                if handle is None:
                    continue
                task = asyncio.create_task(finalize_after_late_drain(run_id, handle))
                late_finalize_tasks.add(task)
                task.add_done_callback(late_finalize_tasks.discard)
        agent_run_id = requested_run_id or (
            distinct_run_ids[-1] if distinct_run_ids else None
        )
        event_data = {
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "status": (
                "cancellation_pending" if drain_timed_out_run_ids else "cancelled"
            ),
            "message": (
                "停止処理を継続しています"
                if drain_timed_out_run_ids
                else "応答生成を停止しました"
            ),
            "cancelled": cancelled,
            "persistence_failed": bool(persistence_failed_run_ids),
        }
        if persistence_failed_run_ids:
            event_data["persistence_failed_run_ids"] = persistence_failed_run_ids
        if finalized_messages:
            finalized_message = finalized_messages[-1]
            event_data["message_id"] = finalized_message.get("id")
            event_data["content"] = finalized_message.get("content", "")
            event_data["metadata"] = finalized_message.get("metadata", {})
            event_data["messages"] = finalized_messages
        await self.broadcast_stream_event("stream_cancelled", event_data)
        logger.info(
            "Stop generation requested: session=%s cancelled=%s", key, cancelled
        )
        result = {
            "session_id": session_id,
            "cancelled": cancelled,
            "agent_run_id": agent_run_id,
            "agent_run_ids": distinct_run_ids,
            "status": event_data["status"],
        }
        if persistence_failed_run_ids:
            result["persistence_failed"] = True
            result["persistence_failed_run_ids"] = persistence_failed_run_ids
        if finalized_messages:
            result["message"] = finalized_messages[-1]
            result["messages"] = finalized_messages
        return result

    async def _persist_steering_user_message(
        self,
        *,
        session_id: Optional[str],
        message: str,
        client_message_id: str,
        sender_user_id: str,
        sender_display_name: str,
        agent_run_id: str,
    ) -> tuple[Optional[str], bool, str, str]:
        from ...assistant.chat_turn_persistence import ChatTurnPersistence

        persistence = ChatTurnPersistence()
        deterministic_message_id = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    ":".join(
                        (
                            "aoitalk-steering",
                            str(session_id or ""),
                            sender_user_id,
                            client_message_id,
                        )
                    ),
                )
            )
            if session_id and sender_user_id and client_message_id
            else ""
        )

        def validate_existing(existing: Any) -> None:
            metadata = getattr(existing, "message_metadata", None) or {}
            if (
                str(getattr(existing, "session_id", "")) != str(session_id or "")
                or str(getattr(existing, "role", "")) != "user"
                or str(getattr(existing, "sender_id", "") or "") != sender_user_id
                or str(getattr(existing, "content", "")) != message
                or metadata.get("client_message_id") != client_message_id
                or metadata.get("delivery_mode") != "immediate_interrupt"
            ):
                raise ValueError("client_message_id conflicts with another message")

        if deterministic_message_id:
            existing = await persistence.load_message(deterministic_message_id)
            if existing is not None:
                validate_existing(existing)
                existing_metadata = getattr(existing, "message_metadata", None) or {}
                return (
                    str(existing.id),
                    False,
                    str(existing_metadata.get("interrupt_receipt_status") or ""),
                    str(existing_metadata.get("agent_run_id") or ""),
                )

        metadata = {
            **(
                {"client_message_id": client_message_id}
                if client_message_id
                else {}
            ),
            "delivery_mode": "immediate_interrupt",
            "delivery_status": "interrupting",
            "interrupt_receipt_status": "pending",
            "agent_run_id": agent_run_id,
        }
        try:
            persisted = await persistence.save_user_message(
                session_id=session_id,
                content=message,
                metadata=metadata,
                sender_type="user",
                sender_id=sender_user_id or None,
                sender_display_name=sender_display_name or sender_user_id or None,
                message_id=deterministic_message_id or None,
            )
        except Exception:
            if not deterministic_message_id:
                raise
            existing = await persistence.load_message(deterministic_message_id)
            if existing is None:
                raise
            validate_existing(existing)
            existing_metadata = getattr(existing, "message_metadata", None) or {}
            return (
                str(existing.id),
                False,
                str(existing_metadata.get("interrupt_receipt_status") or ""),
                str(existing_metadata.get("agent_run_id") or ""),
            )
        return (
            (str(persisted.id), True, "pending", agent_run_id)
            if persisted is not None
            else (None, False, "", "")
        )

    async def _rollback_steering_user_message(
        self,
        *,
        session_id: Optional[str],
        message_id: Optional[str],
    ) -> bool:
        from ...assistant.chat_turn_persistence import ChatTurnPersistence

        return await ChatTurnPersistence().delete_message(
            session_id=session_id,
            message_id=message_id,
        )

    async def _commit_steering_user_message_receipt(
        self,
        *,
        session_id: Optional[str],
        message_id: Optional[str],
    ) -> bool:
        from ...assistant.chat_turn_persistence import ChatTurnPersistence

        return await ChatTurnPersistence().update_message_metadata(
            session_id=session_id,
            message_id=message_id,
            updates={"interrupt_receipt_status": "committed"},
        )

    async def _handle_steer_generation(
        self,
        data: dict,
        *,
        _receipt_locked: bool = False,
    ) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        message = str(data.get("message") or data.get("instruction") or "").strip()
        client_message_id = str(
            data.get("client_message_id") or data.get("instruction_id") or ""
        ).strip()
        requested_run_id = str(data.get("agent_run_id") or "").strip()
        sender_user_id = str(data.get("_sender_user_id") or "").strip()
        sender_display_name = str(data.get("_sender_display_name") or "").strip()
        if not message:
            return {"session_id": session_id, "queued": False}

        key = self._conversation_control_key(session_id)
        receipt_key = (key, sender_user_id, client_message_id)
        if client_message_id and not _receipt_locked:
            steering_locks = getattr(self, "_conversation_steering_locks", None)
            if steering_locks is None:
                steering_locks = {}
                self._conversation_steering_locks = steering_locks
            lock_entry = steering_locks.setdefault(
                receipt_key,
                {"lock": asyncio.Lock(), "users": 0},
            )
            lock_entry["users"] += 1
            try:
                async with lock_entry["lock"]:
                    return await self._handle_steer_generation(
                        data,
                        _receipt_locked=True,
                    )
            finally:
                lock_entry["users"] -= 1
                if (
                    lock_entry["users"] == 0
                    and steering_locks.get(receipt_key) is lock_entry
                ):
                    # users counts the holder and every waiter, so removal can
                    # never occur in asyncio.Lock's release-to-waiter handoff
                    # window.
                    steering_locks.pop(receipt_key, None)
        steering_results = getattr(self, "_conversation_steering_results", None)
        if steering_results is None:
            steering_results = {}
            self._conversation_steering_results = steering_results
        steering_fingerprints = getattr(
            self, "_conversation_steering_fingerprints", None
        )
        if steering_fingerprints is None:
            steering_fingerprints = {}
            self._conversation_steering_fingerprints = steering_fingerprints

        async def reject_steering(
            reason: str,
            event_message: str,
            *,
            run_id: str = "",
        ) -> Dict[str, Any]:
            steering_event = {
                "session_id": session_id,
                "status": "rejected",
                "message": event_message,
            }
            if run_id:
                steering_event["agent_run_id"] = run_id
            if client_message_id:
                steering_event["client_message_id"] = client_message_id
            await self.broadcast_stream_event("steering_update", steering_event)

            result = {
                "session_id": session_id,
                "queued": False,
                "interrupted": False,
                "blocked": True,
                "status": reason,
            }
            if run_id:
                result["agent_run_id"] = run_id
            if client_message_id:
                result["client_message_id"] = client_message_id
            return result

        cached_result = steering_results.get(receipt_key) if client_message_id else None
        if cached_result is not None:
            if steering_fingerprints.get(receipt_key) != message:
                return await reject_steering(
                    "client_message_conflict",
                    "同じ送信IDで異なる割り込みメッセージを受け取りました",
                    run_id=str(cached_result.get("agent_run_id") or requested_run_id),
                )
            duplicate_event = {
                "session_id": session_id,
                "agent_run_id": cached_result.get("agent_run_id"),
                "status": "interrupting",
                "message": "割り込みメッセージは送信済みです",
                "client_message_id": client_message_id,
            }
            await self.broadcast_stream_event("steering_update", duplicate_event)
            return {**cached_result, "duplicate": True}

        status = self.get_conversation_generation_status(session_id)
        if status.get("status") in {
            "cancellation_pending",
            "cancellation_failed",
        }:
            blocked_status = str(status.get("status") or "cancellation_pending")
            return await reject_steering(
                blocked_status,
                "停止処理中のため現在の応答へ割り込めません",
                run_id=requested_run_id,
            )

        # Ctrl+Enter is an active steer while a generation is running.  Resolve
        # the run id from the status first, then fall back to task metadata for
        # the short hand-off window before stream_start has been broadcast.
        candidate_run_ids: list[str] = []
        status_run_id = str(status.get("agent_run_id") or "").strip()
        if status_run_id and (
            not requested_run_id or status_run_id == requested_run_id
        ):
            candidate_run_ids.append(status_run_id)
        for task in getattr(self, "_conversation_generation_tasks", {}).get(key, set()):
            task_run_id = str(getattr(task, "_agent_run_id", "") or "").strip()
            if (
                task_run_id
                and (not requested_run_id or task_run_id == requested_run_id)
                and task_run_id not in candidate_run_ids
            ):
                candidate_run_ids.append(task_run_id)

        if requested_run_id and not candidate_run_ids:
            return await reject_steering(
                "not_found",
                "割り込み対象の応答が見つかりません",
                run_id=requested_run_id,
            )
        if not requested_run_id and len(candidate_run_ids) > 1:
            return await reject_steering(
                "ambiguous_run",
                "割り込み対象の応答を特定できません",
            )

        for run_id in candidate_run_ids:
            reservation = reserve_generation_interrupt(run_id, message)
            if reservation is None:
                continue
            reservation_committed = False
            try:
                persisted_user_message_id = None
                persisted_created = False
                interrupt_receipt_status = ""
                durable_agent_run_id = ""
                try:
                    persist_result = await self._persist_steering_user_message(
                        session_id=session_id,
                        message=message,
                        client_message_id=client_message_id,
                        sender_user_id=sender_user_id,
                        sender_display_name=sender_display_name,
                        agent_run_id=run_id,
                    )
                    persisted_user_message_id, persisted_created = persist_result[:2]
                    interrupt_receipt_status = (
                        str(persist_result[2]) if len(persist_result) > 2 else ""
                    )
                    durable_agent_run_id = (
                        str(persist_result[3]) if len(persist_result) > 3 else ""
                    )
                    if persisted_user_message_id is None:
                        raise RuntimeError("steering user message was not persisted")
                except ValueError:
                    logger.warning(
                        "Conflicting steering client message id: session=%s run=%s",
                        key,
                        run_id,
                    )
                    return await reject_steering(
                        "client_message_conflict",
                        "同じ送信IDで異なる割り込みメッセージを受け取りました",
                        run_id=run_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist steering user message: session=%s run=%s",
                        key,
                        run_id,
                    )
                    return await reject_steering(
                        "persistence_failed",
                        "割り込みメッセージを会話履歴へ保存できませんでした",
                        run_id=run_id,
                    )

                if not persisted_created:
                    existing_result = steering_results.get(receipt_key)
                    if existing_result is not None:
                        duplicate_event = {
                            "session_id": session_id,
                            "agent_run_id": existing_result.get("agent_run_id")
                            or run_id,
                            "status": "interrupting",
                            "message": "割り込みメッセージは送信済みです",
                        }
                        if client_message_id:
                            duplicate_event["client_message_id"] = client_message_id
                        await self.broadcast_stream_event(
                            "steering_update", duplicate_event
                        )
                        return {**existing_result, "duplicate": True}
                    abort_generation_interrupt(reservation)
                    if interrupt_receipt_status == "committed":
                        committed_run_id = durable_agent_run_id or run_id
                        durable_result = {
                            "session_id": session_id,
                            "queued": False,
                            "interrupted": True,
                            "agent_run_id": committed_run_id,
                            "user_message_id": persisted_user_message_id,
                        }
                        if client_message_id:
                            durable_result["client_message_id"] = client_message_id
                        steering_results[receipt_key] = dict(durable_result)
                        steering_fingerprints[receipt_key] = message
                        if len(steering_results) > 512:
                            oldest_receipt_key = next(iter(steering_results))
                            steering_results.pop(oldest_receipt_key, None)
                            steering_fingerprints.pop(oldest_receipt_key, None)
                        await self.broadcast_stream_event(
                            "steering_update",
                            {
                                "session_id": session_id,
                                "agent_run_id": committed_run_id,
                                "status": "interrupting",
                                "message": "割り込みメッセージは送信済みです",
                                "client_message_id": client_message_id,
                            },
                        )
                        return {**durable_result, "duplicate": True}
                    return await reject_steering(
                        "receipt_pending",
                        "前回の割り込み結果を確認できないため再送を停止しました",
                        run_id=run_id,
                    )

                reservation_committed = commit_generation_interrupt(reservation)
                if not reservation_committed:
                    try:
                        rolled_back = await self._rollback_steering_user_message(
                            session_id=session_id,
                            message_id=persisted_user_message_id,
                        )
                        if not rolled_back:
                            logger.error(
                                "Failed to roll back rejected steering message: "
                                "session=%s run=%s message=%s",
                                key,
                                run_id,
                                persisted_user_message_id,
                            )
                    except Exception:
                        logger.exception(
                            "Failed to roll back rejected steering message: "
                            "session=%s run=%s message=%s",
                            key,
                            run_id,
                            persisted_user_message_id,
                        )
                    return await reject_steering(
                        "not_active",
                        "割り込み対象の応答が終了しました",
                        run_id=run_id,
                    )

                # Record the success receipt before the first await after the
                # reservation commit. A route cancellation can no longer
                # create a same-process cache miss that re-applies the steer.
                result = {
                    "session_id": session_id,
                    "queued": False,
                    "interrupted": True,
                    "agent_run_id": run_id,
                    "user_message_id": persisted_user_message_id,
                }
                if client_message_id:
                    result["client_message_id"] = client_message_id
                steering_results[receipt_key] = dict(result)
                steering_fingerprints[receipt_key] = message

                try:
                    receipt_committed = (
                        await self._commit_steering_user_message_receipt(
                            session_id=session_id,
                            message_id=persisted_user_message_id,
                        )
                        if interrupt_receipt_status == "pending"
                        else True
                    )
                    if not receipt_committed:
                        logger.error(
                            "Failed to commit durable steering receipt: "
                            "session=%s run=%s message=%s",
                            key,
                            run_id,
                            persisted_user_message_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to commit durable steering receipt: "
                        "session=%s run=%s message=%s",
                        key,
                        run_id,
                        persisted_user_message_id,
                    )

                persisted_event = {
                    "session_id": session_id,
                    "role": "user",
                    "message_id": persisted_user_message_id,
                    "agent_run_id": run_id,
                }
                if client_message_id:
                    persisted_event["client_message_id"] = client_message_id
                await self.broadcast_stream_event(
                    "conversation_persisted", persisted_event
                )

                steering_event = {
                    "session_id": session_id,
                    "agent_run_id": run_id,
                    "status": "interrupting",
                    "message": "現在の応答を中断して追加指示を反映します",
                }
                if client_message_id:
                    steering_event["client_message_id"] = client_message_id
                await self.broadcast_stream_event("steering_update", steering_event)
                logger.info(
                    "Active generation interrupted for steering: session=%s run=%s",
                    key,
                    run_id,
                )
                if len(steering_results) > 512:
                    oldest_receipt_key = next(iter(steering_results))
                    steering_results.pop(oldest_receipt_key, None)
                    steering_fingerprints.pop(oldest_receipt_key, None)
                return result
            finally:
                if not reservation_committed:
                    # asyncio.CancelledError is not an Exception on supported
                    # Python versions.  Always resolve an uncommitted
                    # reservation so a worker waiting at its checkpoint cannot
                    # hang forever when the route task is cancelled.
                    abort_generation_interrupt(reservation)

        # Ctrl+Enter is exclusively an immediate interrupt. If the requested
        # run is no longer active, do not silently turn it into a queued steer.
        rejected_run_id = requested_run_id or status_run_id
        logger.info("Steering instruction rejected: session=%s", key)
        return await reject_steering(
            "not_active",
            "割り込み対象の応答が見つかりません",
            run_id=rejected_run_id,
        )

    def consume_generation_steering(self, session_id: Optional[str]) -> List[str]:
        key = self._conversation_control_key(session_id)
        return self._conversation_steering_queues.pop(key, [])

    def get_voice_input_session_id(self) -> Optional[str]:
        context = self.get_voice_input_session_context()
        return context.get("session_id") if context else None

    def get_voice_input_session_context(self) -> Optional[Dict[str, Optional[str]]]:
        context_resolver = getattr(self.manager, "get_latest_session_context", None)
        if callable(context_resolver):
            return context_resolver()

        resolver = getattr(self.manager, "get_latest_session_id", None)
        if callable(resolver):
            session_id = resolver()
            if session_id:
                return {"session_id": session_id, "user_id": None}
        return None

    async def dispatch_voice_message(self, message: str) -> bool:
        """Route a local voice transcription into the active WebUI chat session."""
        text = str(message or "").strip()
        if not text:
            return False

        context = self.get_voice_input_session_context()
        session_id = context.get("session_id") if context else None
        if not session_id:
            logger.warning("Voice input skipped WebUI dispatch: no active session")
            return False
        sender_user_id = str((context or {}).get("user_id") or "default_user")

        await self._handle_user_message(
            {
                "message": text,
                "session_id": session_id,
                "_sender_user_id": sender_user_id,
                "_sender_display_name": sender_user_id,
                **(
                    {"_trusted_legacy": TRUSTED_LEGACY_MARKER}
                    if getattr(self, "auth_enabled", True) is False
                    else {}
                ),
            }
        )
        return True

    def _normalize_websocket_images(self, raw_images: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_images, list):
            return []
        return normalize_image_payloads(raw_images)

    def _normalize_websocket_audio(self, raw_audio: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw_audio, dict):
            return None
        payload = raw_audio.get("data") or raw_audio.get("dataUrl")
        if not isinstance(payload, str) or not payload:
            return None
        return {
            "data": payload,
            "mimeType": raw_audio.get("mimeType") or raw_audio.get("mime_type"),
            "name": raw_audio.get("name"),
        }

    def _normalize_websocket_video(self, raw_video: Any) -> Optional[dict[str, Any]]:
        """Normalize a stored video path (or a small direct data payload)."""
        if not isinstance(raw_video, dict):
            return None
        path = raw_video.get("path")
        data = raw_video.get("data") or raw_video.get("dataUrl")
        if not isinstance(path, str) or not path.strip():
            path = None
        if not isinstance(data, str) or not data:
            data = None
        if not path and not data:
            return None
        size = raw_video.get("size")
        if isinstance(size, bool):
            size = None
        elif isinstance(size, (int, float)):
            size = max(0, int(size))
        else:
            size = None
        return {
            **({"path": path.strip()} if path else {}),
            **({"data": data} if data else {}),
            "mimeType": raw_video.get("mimeType") or raw_video.get("mime_type"),
            "name": raw_video.get("name"),
            "size": size,
        }

    def _main_model_supports_vision(self) -> bool | None:
        provider = str(self.config.get("llm_provider", "") or "").strip()
        model = str(self.config.get("llm_model", "") or "").strip()
        return model_supports_vision(provider, model)
