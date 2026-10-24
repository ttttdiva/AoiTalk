#!/usr/bin/env python3
"""
FastAPI + WebSocket server for AoiTalk Web Interface

WebChatServer 本体（合成クラス）。実際の振る舞いは server_parts/ 配下の Mixin へ委譲し、
このファイルには __init__・ライフサイクル・ルート登録オーケストレーションのみを残す。
モジュールレベルの import / 可用性フラグ / logger は server_shared に集約している。
"""

import ipaddress
import inspect
import os
import re
from typing import Mapping
from urllib.parse import urlsplit
from sqlalchemy import select

from .server_shared import *  # noqa: F401,F403
from ..features import Features
from ..runtime import AsyncResourceScope
from src.utils.startup_timing import get_startup_timer
from .server_parts import (
    AuthMixin,
    ChatMessageMixin,
    ConversationMixin,
    MessagingMixin,
    MobileCommandsMixin,
)


_startup_timer = get_startup_timer()


_CORS_DOMAIN_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def _normalize_cors_origin(value: Any) -> str | None:
    """Return a canonical credential-safe HTTP(S) origin or reject it."""
    text = str(value or "").strip()
    if not text or text == "*" or any(character.isspace() for character in text):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None

    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        return None

    try:
        normalized_host = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    if (
        not normalized_host
        or normalized_host.startswith(".")
        or normalized_host.endswith(".")
    ):
        return None

    is_ipv6 = ":" in normalized_host
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if is_ipv6 or all(
            character.isdigit() or character == "." for character in normalized_host
        ):
            return None
        labels = normalized_host.split(".")
        if len(normalized_host) > 253 or not all(
            _CORS_DOMAIN_LABEL_RE.fullmatch(label) for label in labels
        ):
            return None
    else:
        normalized_host = address.compressed
        is_ipv6 = address.version == 6

    authority = f"[{normalized_host}]" if is_ipv6 else normalized_host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _register_verification_provenance_middleware(
    app: FastAPI,
    server: "WebChatServer",
) -> None:
    """Install the production verification provenance request boundary."""
    from ..services.verification_provenance import (
        VerificationProvenanceError,
        VerificationProvenanceService,
    )
    from ..verification.context import (
        context_from_run,
        reset_current_verification_context,
        set_current_verification_context,
        verify_verification_headers,
    )

    @app.middleware("http")
    async def _verification_provenance_middleware(request: Request, call_next):
        try:
            context = verify_verification_headers(request.headers)
        except (TypeError, ValueError):
            return JSONResponse(
                {
                    "detail": "Invalid verification provenance headers",
                    "code": "verification_headers_invalid",
                },
                status_code=400,
            )
        if context is None:
            return await call_next(request)

        manager = server._db_manager
        if manager is None or not callable(getattr(manager, "get_session", None)):
            return JSONResponse(
                {
                    "detail": "Verification provenance is unavailable",
                    "code": "verification_unavailable",
                },
                status_code=503,
            )

        session = None
        token = None
        try:
            session = await manager.get_session()
            run = await VerificationProvenanceService().start_run(
                session,
                run_id=context.run_id,
                source=context.source,
                harness=context.harness,
                commit=True,
            )
            # A completed run cannot be reused to create new domain rows.
            # Cleanup/preview retries are read/maintenance operations and
            # are allowed through without rebinding a write context.
            if run.status != "running":
                path = request.url.path.rstrip("/")
                if path != "/api/admin/verification-data" and not path.startswith(
                    "/api/admin/verification-data/"
                ):
                    return JSONResponse(
                        {
                            "detail": "Verification run is no longer active",
                            "code": "verification_run_closed",
                        },
                        status_code=409,
                    )
                return await call_next(request)

            # Bind the durable row's canonical attribution/timestamp,
            # rather than reusing mutable header text for entity markers.
            token = set_current_verification_context(context_from_run(run))
            return await call_next(request)
        except VerificationProvenanceError:
            if session is not None and callable(getattr(session, "rollback", None)):
                await session.rollback()
            return JSONResponse(
                {
                    "detail": "Verification provenance is unavailable",
                    "code": "verification_unavailable",
                },
                status_code=503,
            )
        except Exception:
            if session is not None and callable(getattr(session, "rollback", None)):
                await session.rollback()
            logger.exception("Failed to establish verification provenance context")
            return JSONResponse(
                {
                    "detail": "Verification provenance is unavailable",
                    "code": "verification_unavailable",
                },
                status_code=503,
            )
        finally:
            if token is not None:
                reset_current_verification_context(token)
            if session is not None and callable(getattr(session, "close", None)):
                await session.close()


class WebChatServer(
    AuthMixin,
    ChatMessageMixin,
    ConversationMixin,
    MessagingMixin,
    MobileCommandsMixin,
):
    """FastAPI-based web chat server"""

    def __init__(self, config, character_name: str):
        self.config = config
        self.character_name = character_name
        self._static_mounts_registered = False
        # Next.js frontend runs on separate port (default 3002)
        self._nextjs_url = os.environ.get("NEXTJS_URL", "http://127.0.0.1:3002")

        # Store reference to self for lifespan to use (set before app creation)
        self._db_manager_for_lifespan = None

        # Heartbeat runner reference
        self._heartbeat_runner = None
        self._heartbeat_execution_dispatcher = None
        self._agent_work_coordinator = None
        self._agent_work_coordinator_started = False
        # Optional CLI development session facade.  The route is registered
        # even when this service is disabled; startup attaches it to the one
        # shared AgentWork coordinator only after the coordinator exists.
        self._cli_development_session_service = None
        self._cli_development_session_work_coordinator = None
        self._task_notification_worker = None
        try:
            from ..heartbeat.runner import get_heartbeat_runner

            heartbeat_config = (
                config.get("heartbeat", {}) if hasattr(config, "get") else {}
            )
            if heartbeat_config.get("enabled", True):
                self._heartbeat_runner = get_heartbeat_runner()
                if self._heartbeat_runner is not None:
                    set_privacy_config = getattr(
                        self._heartbeat_runner, "set_privacy_config", None
                    )
                    if callable(set_privacy_config):
                        set_privacy_config(config)
        except Exception as e:
            logger.warning(f"Heartbeat runner initialization skipped: {e}")
        # FastAPI lifespan owns the resources started by the hook queues
        # below.  Keep the legacy lists as the registration surface used by
        # route modules, while retaining enough metadata to roll back only
        # hooks whose corresponding startup completed successfully.
        self._lifespan_scope: AsyncResourceScope | None = None
        # Alias retained for lifecycle introspection and consistency with
        # other resource-owning services.
        self._resource_scope: AsyncResourceScope | None = None
        self._lifespan_run_count = 0
        self._lifespan_shutdown_complete = False
        self._lifespan_startup_failed = False
        self._lifespan_dispatch_started = False
        self._lifespan_heartbeat_started = False
        self._lifespan_notification_worker_started = False
        self._lifespan_startup_tasks: dict[asyncio.Task[Any], tuple[Any, Any | None]] = {}
        self._lifespan_scheduled_shutdown_ids: set[int] = set()
        self._lifespan_started_shutdown_ids: set[int] = set()
        self._lifecycle_startup_shutdown_pairs: dict[int, Any] = {}
        self._startup_background_tasks: list[Any] = []
        self._shutdown_background_tasks: list[Any] = []
        self._content_retention_worker = None
        try:
            from ..services.content_retention_worker import ContentRetentionWorker

            self._content_retention_worker = ContentRetentionWorker()
            self._register_lifecycle_pair(
                self._content_retention_worker.start,
                self._content_retention_worker.stop,
            )
        except Exception as exc:
            # Retention housekeeping is intentionally optional during a
            # rolling deploy where the Python task purge helper may not yet be
            # importable.  File operations themselves remain fail-closed.
            logger.warning("コンテンツ保持期間ワーカーを登録できませんでした: %s", exc)
        self._mage_vl_preload_factory: Any = None
        self._mage_vl_preload_task: asyncio.Task[Any] | None = None
        self._register_mage_vl_lifecycle()
        self._register_gpu_admission_lifecycle()
        self._register_docs_index_lifecycle()
        self._register_media_index_lifecycle()
        self._register_files_index_lifecycle()
        self._conversation_dispatch_tasks: set[Any] = set()
        self._conversation_dispatch_recovery_task: Any | None = None
        self._conversation_dispatch_shutting_down = False
        self._conversation_generation_tasks: Dict[str, Set[Any]] = {}
        self._conversation_generation_status: Dict[str, Dict[str, Any]] = {}
        self._conversation_steering_queues: Dict[str, List[str]] = {}
        self._conversation_late_finalize_tasks: set[asyncio.Task[Any]] = set()

        # Create lifespan context manager
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """Lifespan event handler for startup/shutdown"""
            scope = AsyncResourceScope("web-chat-server")
            self._lifespan_scope = scope
            self._resource_scope = scope
            self._lifespan_shutdown_complete = False
            self._lifespan_startup_failed = False
            self._lifespan_dispatch_started = False
            self._lifespan_heartbeat_started = False
            self._lifespan_notification_worker_started = False
            if self._lifespan_run_count and not self._startup_background_tasks:
                templates = getattr(
                    self,
                    "_startup_background_task_templates",
                    (),
                )
                self._startup_background_tasks.extend(templates)
            if self._lifespan_run_count and not self._shutdown_background_tasks:
                templates = getattr(
                    self,
                    "_shutdown_background_task_templates",
                    (),
                )
                self._shutdown_background_tasks.extend(templates)
            self._lifespan_startup_tasks.clear()
            self._lifespan_scheduled_shutdown_ids.clear()
            self._lifespan_started_shutdown_ids.clear()

            # A FastAPI test/runtime may reuse the same app for another
            # lifespan after a clean shutdown.  Re-register the retained
            # callback owner before startup; CharacterSwitchManager itself
            # de-duplicates an already-active callback.
            manager = getattr(self, "_character_switch_manager", None)
            callback = getattr(
                self,
                "_character_switch_callback",
                getattr(self, "_on_character_switch", None),
            )
            register = getattr(manager, "register_callback", None)
            if callable(register) and callback is not None:
                try:
                    register(callback)
                except Exception as exc:
                    logger.warning("Character switch callback re-registration failed: %s", exc)

            # Keep shutdown order explicit.  The dispatch service may still
            # need the notification/heartbeat infrastructure while it drains,
            # so a generic AsyncExitStack LIFO order would be unsafe here.
            scope.add_async_cleanup(self._shutdown_lifespan_resources)
            # The callback is registered during server construction, before
            # this lifespan exists.  Release it through the scope so startup
            # rollback cannot leave a process-global CharacterSwitchManager
            # retaining this server instance.
            scope.add_cleanup(self._release_character_switch_callback)
            startup_succeeded = False
            try:
                # Startup order intentionally mirrors the historical
                # composition root.
                with _startup_timer.phase("startup.web.lifespan.on_startup"):
                    await self._on_startup()
                with _startup_timer.phase("startup.web.lifespan.dispatch_recovery"):
                    # Treat a dispatch start attempt as owned before awaiting
                    # it: implementations may allocate resources and then
                    # raise, and their idempotent stop path must still run on
                    # startup rollback.
                    self._lifespan_dispatch_started = True
                    await self._start_conversation_dispatch_recovery()
                # Start heartbeat runner
                if self._heartbeat_runner:
                    try:
                        self._lifespan_heartbeat_started = True
                        with _startup_timer.phase("startup.web.lifespan.heartbeat_start"):
                            await self._heartbeat_runner.start()
                        logger.info("Heartbeat runner started")
                    except Exception as e:
                        logger.error(f"Heartbeat runner start failed: {e}")
                if self._task_notification_worker:
                    try:
                        self._lifespan_notification_worker_started = True
                        with _startup_timer.phase("startup.web.lifespan.notification_worker_start"):
                            await self._task_notification_worker.start()
                    except Exception as e:
                        logger.error(f"Task notification worker start failed: {e}")
                startup_succeeded = True
                yield
            except BaseException:
                # ``scope.aclose`` below cancels/awaits every startup task and
                # invokes the explicit resource cleanup callback.  Marking the
                # phase as failed lets that callback skip hooks whose start
                # coroutine never completed.
                self._lifespan_startup_failed = not startup_succeeded
                raise
            finally:
                try:
                    await scope.aclose()
                finally:
                    if self._lifespan_scope is scope:
                        self._lifespan_scope = None
                    if self._resource_scope is scope:
                        self._resource_scope = None
                    self._lifespan_run_count += 1

        self.app = FastAPI(title="AoiTalk Web Interface", lifespan=lifespan)

        from .http_errors import register_http_error_handlers

        register_http_error_handlers(self.app)

        # Debug logging
        logger.info(f"WebChatServer initialized with character: {character_name}")
        logger.info(f"Config type: {type(config)}")
        if hasattr(config, "config"):
            logger.info(f"Config has 'config' attribute")

        # キャラクター切り替え通知の登録
        self._register_character_switch_callback()
        if not getattr(self, "_character_switch_manager", None):
            try:
                # Compatibility fallback for older mixins that do not retain
                # their callback owner themselves.
                self._character_switch_manager = CharacterSwitchManager()
            except Exception:
                self._character_switch_manager = None

        # Add CORS middleware
        # allow_credentials=True と allow_origins=["*"] の併用は CORS 仕様上無効なため、
        # 許可オリジンを明示する（AOITALK_CORS_ORIGINS 環境変数で上書き可能）
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self._build_cors_origins(),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        # 低帯域環境向け: JSON/text 応答の gzip 圧縮を有効化する。
        # Range / 206・media・添付ファイルは部分配信のバイト位置を守るため除外する。
        from .safe_gzip import SafeGZipMiddleware

        self.app.add_middleware(SafeGZipMiddleware, minimum_size=1024)
        # ``UploadFile`` is parsed before endpoint dependencies, so route code
        # cannot stop an unauthenticated oversized multipart body from being
        # spooled.  Keep this as the outermost user middleware and enforce the
        # complete request size at the ASGI receive boundary.
        from .request_body_limits import MultipartBodyLimitMiddleware

        self.app.add_middleware(MultipartBodyLimitMiddleware)
        self._register_static_mounts()

        # Database manager for login logging (must be before auth settings)
        self._db_manager = None
        if get_database_manager is not None:
            try:
                self._db_manager = get_database_manager()
            except Exception as e:
                logger.warning(
                    f"Failed to initialize database manager for login logging: {e}"
                )

        # Verification requests carry a server-signed run identity.  Establish
        # the durable run before dispatching the endpoint and bind the context
        # for the lifetime of the request so every Project/Task/User writer can
        # attach provenance in the same transaction.  A partial or forged
        # header set is rejected here; ordinary requests remain completely
        # unaffected.  The middleware deliberately does not trust request
        # bodies or query parameters for provenance.
        _register_verification_provenance_middleware(self.app, self)

        # Auth settings (depends on _db_manager for DB auth)
        (
            self.auth_enabled,
            self.auth_user,
            self.auth_pass,
            self.auth_secret,
            self.session_ttl_seconds,
        ) = self._load_auth_settings()
        if self.auth_enabled is not True and self.auth_enabled is not False:
            raise RuntimeError(
                "WebUI authentication state must be explicitly enabled or disabled"
            )
        # FastAPI and Next.js use different session formats. Keep their cookie
        # names separate so logging in through one surface does not overwrite
        # the other's session.
        self.cookie_name = os.getenv(
            "AOITALK_FASTAPI_SESSION_COOKIE", "aoitalk_fastapi_session"
        )
        self.next_cookie_name = (
            os.getenv("AOITALK_SESSION_COOKIE_NAME", "aoitalk_session").strip()
            or "aoitalk_session"
        )
        # Keep the old FastAPI fallback aligned with the configured Next.js
        # cookie.  In particular, the disposable QA frontend must not accept
        # the production aoitalk_session cookie shared by the same browser.
        self.legacy_cookie_name = self.next_cookie_name

        # Connection manager
        self.manager = ConnectionManager()
        self.manager.set_authorization_checker(self._websocket_connection_allowed)
        self.manager.set_admin_role_checker(self._websocket_is_admin_user)

        # Install the durable Heartbeat execution callback before lifespan start.
        self._configure_heartbeat_execution()

        # Register BGM change callback
        if set_bgm_callback:

            async def _bgm_broadcast(bgm_id: str, volume: float):
                await self.manager.broadcast(
                    {
                        "type": "bgm_change",
                        "data": {
                            "bgm_id": bgm_id,
                            "volume": volume,
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                        },
                    }
                )

            set_bgm_callback(_bgm_broadcast)
            logger.info("WebChatServer: BGM切り替えコールバックを登録しました")

        if TASK_NOTIFICATION_WORKER_AVAILABLE and self._db_manager is not None:
            self._task_notification_worker = TaskNotificationWorker(
                self._db_manager,
                broadcaster=self.manager.broadcast,
                poll_interval_seconds=self._extract_task_notification_poll_interval(),
                config=self.config,
            )

        # Callbacks
        self.on_user_input = None
        self.on_clear_chat = None  # Callback for clear chat events
        self.on_llm_client_change = None
        self.on_session_llm_route_change = None
        self.main_event_loop = None

        # Voice status
        self.voice_recognition_ready = False
        self.current_rms = 0.0
        self.is_recording = False

        # Duplicate prevention for voice messages
        self._last_user_message = ""
        self._last_user_message_time = 0
        self._duplicate_threshold = 2.0  # seconds

        # Mobile UI settings
        self.mobile_ui_config = self._extract_mobile_ui_config()

        # Login session tracking for calculating session duration
        self._login_sessions: Dict[str, datetime] = {}  # username -> login time

        # Crawler status cache for Push API
        self._crawler_status_cache: Dict[str, Dict[str, Any]] = {}

        # Initialize external LLM permission manager
        self._external_llm_permission_manager = None
        self._permission_broadcast_loop = None
        if EXTERNAL_LLM_PERMISSION_AVAILABLE:
            self._init_external_llm_permission_manager()
        self._human_interaction_manager = None
        self._init_human_interaction_manager()

        # LLM client reference (will be set by terminal/voice mode)
        self._llm_client = None
        self._current_llm_mode = "fast"  # 'fast' or 'thinking'
        self._ollama_model_manager = OllamaModelManager(config)
        from ..services.local_llm_runtime_manager import ManagedLocalRuntimeManager

        self._local_llm_runtime_manager = ManagedLocalRuntimeManager(config)

        self._project_overview_worker = None
        self._register_project_overview_lifecycle()

        self._knowledge_capture_worker = None
        self._register_knowledge_capture_lifecycle()

        self._dreaming_memory_worker = None
        self._register_dreaming_memory_lifecycle()

        # Setup routes
        self._setup_routes()

        # Register project routes if available
        if PROJECT_ROUTES_AVAILABLE and create_project_router:
            self._register_project_routes()

        # Register the Project Overview API independently from the base
        # Project CRUD router.
        if PROJECT_OVERVIEW_ROUTES_AVAILABLE and create_project_overview_router:
            self._register_project_overview_routes()

        # Register Project-scoped Docs candidate review routes immediately
        # after the base Project routes.  These endpoints intentionally do
        # not use the generic memory candidate decision API.
        if (
            PROJECT_DOCS_CANDIDATE_ROUTES_AVAILABLE
            and create_project_docs_candidate_router
        ):
            self._register_project_docs_candidate_routes()

        # Register Resolution Knowledge Capture independently from the legacy
        # Docs candidate queue.  Its optional import boundary keeps startup
        # available during a rolling deployment where the new domain services
        # are not present yet.
        if KNOWLEDGE_CAPTURE_ROUTES_AVAILABLE and create_knowledge_capture_router:
            self._register_knowledge_capture_routes()

        # Register the authenticated Engagement Operations kernel after the
        # project-scoped routes so its service can enforce the same ACL model.
        if OPERATIONS_ROUTES_AVAILABLE and create_operations_router:
            self._register_operations_routes()

        # MediaOps is intentionally adjacent to, but independent from, the
        # EngagementOps Trusted Kernel.
        if (
            MEDIA_OPERATIONS_ROUTES_AVAILABLE
            and create_media_operations_router
        ):
            self._register_media_operations_routes()

        if (
            MEDIA_OPERATIONS_SETUP_ROUTES_AVAILABLE
            and create_media_operations_setup_router
        ):
            self._register_media_operations_setup_routes()

        if (
            MEDIA_OPERATIONS_RESEARCH_ROUTES_AVAILABLE
            and create_media_operations_research_router
        ):
            self._register_media_operations_research_routes()

        if (
            MEDIA_OPERATIONS_GENERATION_ROUTES_AVAILABLE
            and create_media_operations_generation_router
        ):
            self._register_media_operations_generation_routes()

        if (
            MEDIA_OPERATIONS_AUTOMATION_ROUTES_AVAILABLE
            and create_media_operations_automation_router
        ):
            self._register_media_operations_automation_routes()

        if (
            MEDIA_OPERATIONS_CONTENT_ROUTES_AVAILABLE
            and create_media_operations_content_router
        ):
            self._register_media_operations_content_routes()

        if (
            MEDIA_OPERATIONS_METRICS_ROUTES_AVAILABLE
            and create_media_operations_metrics_router
        ):
            self._register_media_operations_metrics_routes()

        if (
            MEDIA_OPERATIONS_LEARNING_ROUTES_AVAILABLE
            and create_media_operations_learning_router
        ):
            self._register_media_operations_learning_routes()

        if (
            MEDIA_OPERATIONS_OVERVIEW_ROUTES_AVAILABLE
            and create_media_operations_overview_router
        ):
            self._register_media_operations_overview_routes()

        # Register Knowledge Workspace routes if available
        if KNOWLEDGE_ROUTES_AVAILABLE and create_knowledge_router:
            self._register_knowledge_routes()

        # Register Deep Research routes if available
        if DEEP_RESEARCH_ROUTES_AVAILABLE and create_deep_research_router:
            self._register_deep_research_routes()

        # Register conversation routes if available
        if CONVERSATION_ROUTES_AVAILABLE and create_conversation_router:
            self._register_conversation_routes()

        self._register_chat_intent_routes()

        # Register group chat routes if available
        if GROUP_CHAT_ROUTES_AVAILABLE and create_group_chat_router:
            self._register_group_chat_routes()

        # Register skill routes if available
        if SKILL_ROUTES_AVAILABLE and create_skill_router:
            self._register_skill_routes()

        # Register skill recording routes if available
        if SKILL_RECORDING_ROUTES_AVAILABLE and create_skill_recording_router:
            self._register_skill_recording_routes()

        # Register task event routes if available
        if TASK_ROUTES_AVAILABLE and create_task_router:
            self._register_task_routes()

        # Register per-user Webex OAuth and read-only messaging routes.
        if WEBEX_ROUTES_AVAILABLE and create_webex_router:
            self._register_webex_routes()

        # Register mobile sync routes after task routes; it reuses task service semantics.
        if (
            not Features.is_enterprise()
            and SYNC_ROUTES_AVAILABLE
            and create_sync_router
        ):
            self._register_sync_routes()

        # Register Docs REST routes (shares apply_docs_operation with sync push).
        if DOCS_ROUTES_AVAILABLE and create_docs_router:
            self._register_docs_routes()

        # Meeting-processing is an authenticated server-to-server backend.
        # Keep all runtime dependencies behind the composition root so the
        # route's readiness response reflects the actual durable worker rather
        # than a second, request-scoped implementation.  Constructors are
        # intentionally lazy: Whisper/torch/model weights are not imported or
        # loaded during server startup.
        self._meeting_processing_storage = None
        self._meeting_processing_whisper = None
        self._meeting_processing_local_llm = None
        self._meeting_processing_docs = None
        self._meeting_processing_worker = None
        if (
            MEETING_PROCESSING_ROUTES_AVAILABLE
            and create_meeting_processing_router
        ):
            try:
                from ..services.meeting_docs_service import MeetingDocsService
                from ..services.meeting_local_llm_service import MeetingLocalLlmService
                from ..services.meeting_processing_storage import MeetingAudioStorage
                from ..services.meeting_processing_worker import MeetingProcessingWorker
                from ..services.meeting_whisper_service import MeetingWhisperService

                meeting_workspace_root = self._resolve_workspace_root()
                self._meeting_processing_storage = MeetingAudioStorage(
                    meeting_workspace_root,
                    defer_staging_cleanup=True,
                )
                self._meeting_processing_whisper = MeetingWhisperService(self.config)
                self._meeting_processing_local_llm = MeetingLocalLlmService(self.config)
                self._meeting_processing_docs = MeetingDocsService(
                    workspace_root=meeting_workspace_root,
                    get_db_manager=lambda: self._db_manager,
                )
                self._meeting_processing_worker = MeetingProcessingWorker(
                    lambda: self._db_manager,
                    config=self.config,
                    workspace_root=meeting_workspace_root,
                    storage=self._meeting_processing_storage,
                    whisper=self._meeting_processing_whisper,
                    local_llm=self._meeting_processing_local_llm,
                    docs=self._meeting_processing_docs,
                )
                self._register_lifecycle_pair(
                    self._meeting_processing_worker.start,
                    self._meeting_processing_worker.stop,
                )
            except Exception as exc:
                # Keep the API surface available while failing closed on
                # /ready and /jobs when an optional runtime dependency cannot
                # be constructed in this process.
                logger.warning(
                    "Meeting-processing worker registration skipped: %s", exc,
                    exc_info=True,
                )

            self.app.include_router(
                create_meeting_processing_router(
                    db_manager=self._db_manager,
                    resolve_long_lived_token=(
                        self._get_user_info_from_long_lived_token
                    ),
                    readiness_provider=(
                        self._meeting_processing_worker.readiness_snapshot
                        if self._meeting_processing_worker is not None
                        else None
                    ),
                    storage=self._meeting_processing_storage,
                )
            )

        # Register authenticated per-user X Cookie management routes.
        if X_COOKIE_ROUTES_AVAILABLE and create_x_cookie_router:
            self._register_x_cookie_routes()

        # Register heartbeat routes if available
        if HEARTBEAT_ROUTES_AVAILABLE and create_heartbeat_router:
            self._register_heartbeat_routes()

        self._register_local_agent_task_routes()
        self._register_cli_development_session_routes()

        # Register agent harness status routes if available
        if AGENT_HARNESS_ROUTES_AVAILABLE and create_agent_harness_router:
            self._register_agent_harness_routes()

        if APPS_ROUTES_AVAILABLE and create_apps_router:
            self._register_apps_routes()

        # Register ECC feature routes if available
        if ECC_ROUTES_AVAILABLE and create_ecc_router:
            self._register_ecc_routes()

        # Register comfyui routes if available
        if COMFYUI_ROUTES_AVAILABLE and create_comfyui_router:
            self._register_comfyui_routes()
        self._register_generated_media_routes()

        # Register the frontend catch-all last so it does not shadow API routers.
        self._register_frontend_catchall()
        # Keep immutable registration templates so a test/runtime that reuses
        # the same FastAPI app for another lifespan can start and stop the
        # same owned workers again.  The queues remain the canonical entries;
        # per-lifespan scope state tracks which hooks were scheduled.
        self._startup_background_task_templates = list(
            self._startup_background_tasks
        )
        self._shutdown_background_task_templates = list(
            self._shutdown_background_tasks
        )

    def _register_lifecycle_pair(self, startup: Any, shutdown: Any) -> None:
        """Register a startup/shutdown pair while preserving legacy queues.

        Several route modules append directly to the public hook lists.  This
        helper is used for pairs owned by the composition root so rollback can
        identify the matching shutdown callback without changing those module
        interfaces.
        """

        startup_tasks = getattr(self, "_startup_background_tasks", None)
        if not isinstance(startup_tasks, list):
            startup_tasks = []
            self._startup_background_tasks = startup_tasks
        shutdown_tasks = getattr(self, "_shutdown_background_tasks", None)
        if not isinstance(shutdown_tasks, list):
            shutdown_tasks = []
            self._shutdown_background_tasks = shutdown_tasks
        startup_tasks.append(startup)
        shutdown_tasks.append(shutdown)
        pairs = getattr(self, "_lifecycle_startup_shutdown_pairs", None)
        if not isinstance(pairs, dict):
            pairs = {}
            self._lifecycle_startup_shutdown_pairs = pairs
        pairs[id(startup)] = shutdown

    def _release_character_switch_callback(self) -> None:
        """Unregister the server callback from the process-global manager."""

        manager = getattr(self, "_character_switch_manager", None)
        callback = getattr(
            self,
            "_character_switch_callback",
            getattr(self, "_on_character_switch", None),
        )
        if manager is None or callback is None:
            return
        unregister = getattr(manager, "unregister_callback", None)
        if not callable(unregister):
            return
        try:
            unregister(callback)
        except Exception as exc:
            logger.warning("Character switch callback cleanup failed: %s", exc)
        finally:
            self._character_switch_callback_registered = False

    def _lifecycle_shutdown_for_startup(self, startup: Any) -> Any | None:
        """Best-effort matching for hooks registered by route modules.

        Composition-root registrations use an explicit identity map.  For
        legacy route modules that still append to both lists independently,
        match bound methods by owner first, then the conventional
        ``start_*``/``stop_*`` names.  Unknown startup-only hooks remain
        task-owned and are cancelled by ``AsyncResourceScope``.
        """

        pairs = getattr(self, "_lifecycle_startup_shutdown_pairs", None)
        if isinstance(pairs, dict):
            shutdown = pairs.get(id(startup))
            if shutdown is not None:
                return shutdown

        shutdown_tasks = getattr(self, "_shutdown_background_tasks", None)
        if not isinstance(shutdown_tasks, list):
            return None
        owner = getattr(startup, "__self__", None)
        if owner is not None:
            for candidate in shutdown_tasks:
                if getattr(candidate, "__self__", None) is owner:
                    return candidate

        startup_name = str(getattr(startup, "__name__", "") or "")
        candidate_names: list[str] = []
        if startup_name.startswith("start_"):
            candidate_names.append(f"stop_{startup_name[6:]}")
        elif startup_name == "start":
            candidate_names.extend(("stop", "close", "shutdown", "cleanup"))
        for candidate in shutdown_tasks:
            if str(getattr(candidate, "__name__", "") or "") in candidate_names:
                return candidate
        return None

    def _spawn_lifespan_task(
        self,
        coro: Any,
        *,
        name: str,
        startup_factory: Any | None = None,
    ) -> asyncio.Task[Any]:
        """Spawn a task owned by the current lifespan scope.

        ``_on_startup`` is also called directly by a few integrations/tests;
        retain a safe fallback for that path while the FastAPI lifespan uses
        ``AsyncResourceScope`` for cancellation and exception retrieval.
        """

        scope = getattr(self, "_lifespan_scope", None)
        if isinstance(scope, AsyncResourceScope):
            task = scope.spawn(coro, name=name)
        else:
            try:
                task = asyncio.create_task(coro, name=name)
            except Exception:
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                raise

        if startup_factory is not None:
            stop_hook = self._lifecycle_shutdown_for_startup(startup_factory)
            startup_tasks = getattr(self, "_lifespan_startup_tasks", None)
            if not isinstance(startup_tasks, dict):
                startup_tasks = {}
                self._lifespan_startup_tasks = startup_tasks
            startup_tasks[task] = (startup_factory, stop_hook)
            if stop_hook is not None:
                scheduled_ids = getattr(
                    self,
                    "_lifespan_scheduled_shutdown_ids",
                    None,
                )
                if isinstance(scheduled_ids, set):
                    # A startup hook may partially acquire a resource before
                    # raising.  Mark the paired stop as eligible immediately
                    # so rollback does not depend on a successful task result.
                    scheduled_ids.add(id(stop_hook))

            def _startup_done(completed: asyncio.Task[Any]) -> None:
                metadata = startup_tasks.pop(completed, None)
                if metadata is None or completed.cancelled():
                    return
                try:
                    error = completed.exception()
                except BaseException:
                    return
                if error is not None:
                    logger.error(
                        "Lifespan startup task failed (%s): %s",
                        name,
                        error,
                    )
                    return
                shutdown = metadata[1]
                if shutdown is not None:
                    started_ids = getattr(
                        self,
                        "_lifespan_started_shutdown_ids",
                        None,
                    )
                    if isinstance(started_ids, set):
                        started_ids.add(id(shutdown))

            task.add_done_callback(_startup_done)
        return task

    async def _shutdown_lifespan_resources(self) -> None:
        """Release every server-owned lifespan resource in dependency order."""

        if getattr(self, "_lifespan_shutdown_complete", False):
            return
        self._lifespan_shutdown_complete = True
        rollback = bool(getattr(self, "_lifespan_startup_failed", False))

        # Preserve the established order: dispatch first, then notification,
        # heartbeat, and finally the hook queue.  Each callback is isolated so
        # one failure never prevents the remaining resources from closing.
        if not rollback or getattr(self, "_lifespan_dispatch_started", False):
            try:
                await self._stop_conversation_dispatch_recovery()
            except BaseException as exc:
                logger.exception("Conversation dispatch shutdown failed: %s", exc)

        coordinator = getattr(self, "_agent_work_coordinator", None)
        if coordinator is not None and (
            not rollback or getattr(self, "_agent_work_coordinator_started", False)
        ):
            try:
                await coordinator.stop()
            except BaseException as exc:
                logger.error("Common AgentWork coordinator stop failed: %s", exc)
            finally:
                self._agent_work_coordinator_started = False

        notification_worker = getattr(self, "_task_notification_worker", None)
        if notification_worker and (
            not rollback
            or getattr(self, "_lifespan_notification_worker_started", False)
        ):
            try:
                await notification_worker.stop()
            except BaseException as exc:
                logger.error(f"Task notification worker stop failed: {exc}")

        heartbeat_runner = getattr(self, "_heartbeat_runner", None)
        if heartbeat_runner and (
            not rollback or getattr(self, "_lifespan_heartbeat_started", False)
        ):
            try:
                await heartbeat_runner.stop()
            except BaseException as exc:
                logger.error(f"Heartbeat runner stop failed: {exc}")

        shutdown_queue = getattr(self, "_shutdown_background_tasks", None)
        # Keep the registration queues as the server's canonical startup
        # contract.  A WebChatServer/TestClient can be started more than once
        # during its lifetime; consuming the lists here would silently omit
        # resources on the second lifespan.  The per-lifespan scope and flags
        # still make each invocation idempotent.
        pending_shutdown_tasks = list(shutdown_queue or [])
        scheduled_ids = getattr(self, "_lifespan_scheduled_shutdown_ids", set())
        for shutdown in pending_shutdown_tasks:
            if rollback and id(shutdown) not in scheduled_ids:
                continue
            try:
                result = shutdown()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                logger.exception("Shutdown task failed: %s", exc)

    def _register_mage_vl_lifecycle(self) -> None:
        """Register lazy Mage-VL warmup and owned-process cleanup."""

        async def _preload_mage_vl() -> None:
            try:
                from ..services.mage_vl_service import get_mage_vl_service

                await get_mage_vl_service(self.config).preload_if_configured()
            except Exception as exc:
                # A missing optional SGLang install must not prevent AoiTalk
                # from serving text/image/audio conversations.
                logger.warning("Mage-VLの事前ロードに失敗しました: %s", exc)

        async def _shutdown_mage_vl() -> None:
            from ..services.mage_vl_service import shutdown_mage_vl_services

            preload_task = self._mage_vl_preload_task
            if preload_task is not None:
                if not preload_task.done():
                    preload_task.cancel()
                try:
                    await preload_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Mage-VL事前ロードの終了処理に失敗しました: %s", exc)
                finally:
                    if self._mage_vl_preload_task is preload_task:
                        self._mage_vl_preload_task = None
            await shutdown_mage_vl_services()

        self._mage_vl_preload_factory = _preload_mage_vl
        self._register_lifecycle_pair(_preload_mage_vl, _shutdown_mage_vl)

    def _build_gpu_lease_backend(self) -> Any:
        """The real trial backend (psutil, nvidia-smi and shared-broker ports), or None.

        Opt-in by ``AOITALK_GPU_TRIAL_PROFILE`` (see ``gpu_admission_factory``). None keeps
        cooperative admission a pass-through watchdog: no lease can be granted, so indexing is
        never paused, and ``_gpu_admission_unavailable_reason`` says why (never "ready").
        Installing a backend starts the coordinator in RECOVERY until surviving trial
        processes are reconciled.
        """
        from ..services.gpu_admission_factory import build_gpu_trial_runtime

        runtime, reason = build_gpu_trial_runtime(self.config)
        self._gpu_trial_runtime = runtime
        self._gpu_admission_unavailable_reason = reason
        return runtime.backend if runtime is not None else None

    def _register_gpu_admission_lifecycle(self) -> None:
        """Own the GPU admission watchdog; registered before any ticket holder starts."""
        from ..services.gpu_admission import configure_gpu_admission
        from ..services.gpu_admission_driver import GpuAdmissionDriver

        backend = self._build_gpu_lease_backend()
        coordinator = configure_gpu_admission(recovering=backend is not None)
        driver = self._gpu_admission_driver = GpuAdmissionDriver(coordinator, backend)
        runtime = getattr(self, "_gpu_trial_runtime", None)
        if runtime is None:
            self._gpu_trial_launcher = None
            self._register_lifecycle_pair(driver.start, driver.stop)
            return
        launcher = self._gpu_trial_launcher = runtime.launcher(driver)

        async def _stop_gpu_admission() -> None:
            # Producers first (they only request cancels), then the watchdog, and the state store
            # last: it must still be there to persist whatever the stopping calls recorded, and its
            # shutdown waits for every write already queued.
            await launcher.aclose()
            await driver.stop()
            await runtime.backend.aclose()

        self._register_lifecycle_pair(driver.start, _stop_gpu_admission)

    def _register_docs_index_lifecycle(self) -> None:
        """Pair the durable Docs index worker with this server lifespan."""
        from ..services.docs_index_worker import DocsIndexWorker
        from ..memory.database import get_db_session

        async def session_factory():
            manager = getattr(self, "_db_manager", None)
            return await manager.get_session() if manager is not None else await get_db_session()

        self._docs_index_worker = DocsIndexWorker(session_factory)
        self._register_lifecycle_pair(self._docs_index_worker.start, self._docs_index_worker.stop)

    def _register_media_index_lifecycle(self) -> None:
        from ..services.media_index_worker import MediaIndexWorker
        from ..memory.database import get_db_session

        async def session_factory():
            manager = getattr(self, "_db_manager", None)
            return await manager.get_session() if manager is not None else await get_db_session()

        self._media_index_worker = MediaIndexWorker(session_factory)
        self._register_lifecycle_pair(self._media_index_worker.start, self._media_index_worker.stop)

    def _register_files_index_lifecycle(self) -> None:
        """Scan User/Project Files into the workspace_file media index inventory."""
        from ..services.workspace_file_index import WorkspaceFileIndexService
        from ..memory.database import get_db_session

        async def session_factory():
            manager = getattr(self, "_db_manager", None)
            return await manager.get_session() if manager is not None else await get_db_session()

        self._files_index_service = WorkspaceFileIndexService(
            session_factory, workspace_root=self._resolve_workspace_root())
        self._register_lifecycle_pair(self._files_index_service.start, self._files_index_service.stop)

    def _register_project_overview_lifecycle(self) -> None:
        """Register the durable Project Overview refresh worker."""

        if getattr(self, "_project_overview_worker", None) is not None:
            return
        if self._db_manager is None:
            logger.warning(
                "Project Overview worker registration skipped: database unavailable"
            )
            return

        try:
            from ..services.project_overview_worker import ProjectOverviewWorker

            self._project_overview_worker = ProjectOverviewWorker(
                config=self.config,
                db_manager=self._db_manager,
                config_loader=self._load_project_overview_config_snapshot,
            )
        except Exception as exc:
            logger.warning(
                "Project Overview worker registration skipped: %s",
                exc,
            )
            self._project_overview_worker = None
            return

        self._register_lifecycle_pair(
            self._project_overview_worker.start,
            self._project_overview_worker.stop,
        )

    def _register_knowledge_capture_lifecycle(self) -> None:
        """Register the optional Knowledge Capture worker.

        The worker owns only durable polling/claim/recovery orchestration;
        candidate research and Docs publication remain in their domain
        services.  Keep construction behind the availability and database
        gates so an older checkout still boots normally.
        """

        if not KNOWLEDGE_CAPTURE_WORKER_AVAILABLE or self._db_manager is None:
            if KNOWLEDGE_CAPTURE_WORKER_AVAILABLE:
                logger.warning(
                    "Knowledge Capture worker registration skipped: database unavailable"
                )
            return

        try:
            self._knowledge_capture_worker = KnowledgeCaptureWorker(
                self._db_manager,
                config=self.config,
            )
        except TypeError:
            # Preserve compatibility with a rolling-deploy worker that uses a
            # keyword-only db_manager constructor.
            try:
                self._knowledge_capture_worker = KnowledgeCaptureWorker(
                    db_manager=self._db_manager,
                    config=self.config,
                )
            except Exception as exc:
                logger.warning(
                    "Knowledge Capture worker registration skipped: %s",
                    exc,
                )
                self._knowledge_capture_worker = None
                return
        except Exception as exc:
            logger.warning(
                "Knowledge Capture worker registration skipped: %s",
                exc,
            )
            self._knowledge_capture_worker = None
            return

        start = getattr(self._knowledge_capture_worker, "start", None)
        stop = getattr(self._knowledge_capture_worker, "stop", None)
        if not callable(start) or not callable(stop):
            logger.warning(
                "Knowledge Capture worker registration skipped: start/stop unavailable"
            )
            self._knowledge_capture_worker = None
            return
        self._register_lifecycle_pair(start, stop)
        logger.info("Knowledge Capture worker registered")

    async def _load_project_overview_config_snapshot(self) -> Any:
        """Load DB-backed Project Automation settings for one worker claim."""

        from ..app_config_store import (
            AppConfigSnapshotUnavailable,
            load_app_config_snapshot_sync,
        )

        try:
            return await asyncio.to_thread(
                load_app_config_snapshot_sync,
                bootstrap_config=self.config,
            )
        except AppConfigSnapshotUnavailable:
            logger.warning(
                "Project Overview config snapshot unavailable: exception_type=%s",
                AppConfigSnapshotUnavailable.__name__,
            )
            raise
        except Exception as exc:
            # A worker must never route a Project Overview request through a
            # stale startup Config after the DB-backed snapshot fails.  Keep
            # the public signal stable and secret-free while retaining the
            # exception type in server logs only.
            logger.warning(
                "Project Overview config snapshot unavailable: exception_type=%s",
                type(exc).__name__,
            )
            raise AppConfigSnapshotUnavailable() from exc

    def _register_dreaming_memory_lifecycle(self) -> None:
        """Register the optional Dreaming consolidator without eager LLM use.

        Dreaming runs only when the process has an active chat client.  The
        worker therefore receives a factory rather than a client instance and
        resolves it on each user run.  Imports stay local so a rolling deploy
        with the new worker/migration not yet installed can still start the
        web server and serve the legacy memory endpoints.
        """
        if getattr(self, "_dreaming_memory_worker", None) is not None:
            return

        worker_type = None
        try:
            from ..services.dreaming_memory_worker import DreamingMemoryWorker

            worker_type = DreamingMemoryWorker
        except ImportError:
            # Keep the historical module layout as a rolling-deploy fallback,
            # but make both candidates literal so the Enterprise import-closure
            # checker can prove the complete internal dependency set.
            try:
                from ..services.dreaming_consolidation_service import DreamingMemoryWorker

                worker_type = DreamingMemoryWorker
            except (ImportError, AttributeError):
                worker_type = None
            except Exception as exc:
                logger.warning("Dreaming Memory worker import skipped: %s", exc)
                return
        except Exception as exc:
            logger.warning("Dreaming Memory worker import skipped: %s", exc)
            return
        if worker_type is None:
            return

        owned_background_client: dict[str, Any] = {"client": None}

        def _llm_client_factory(*args: Any, **kwargs: Any) -> Any:
            del args
            client = getattr(self, "_llm_client", None)
            if client is None:
                client = owned_background_client.get("client")
                if client is None:
                    try:
                        from ..llm.manager import create_llm_client

                        client = create_llm_client(self.config)
                        owned_background_client["client"] = client
                    except Exception:
                        logger.warning("Dreaming owned LLM client creation unavailable")
                        return None
            # Reuse the same detached per-turn state reset as Scoped Memory
            # jobs.  This prevents consolidation usage/tool/history snapshots
            # from being appended to the foreground conversation client.
            try:
                from ..services.scoped_memory_job_service import _scoped_memory_llm_client

                isolated = _scoped_memory_llm_client(
                    client,
                    user_id=kwargs.get("user_id"),
                    session_id=kwargs.get("session_id"),
                    session_context=dict(kwargs.get("session_context") or {}),
                    project_metadata=dict(kwargs.get("project_metadata") or {}),
                )
                return isolated if isolated is not client else None
            except Exception:
                # Never hand the foreground client to a background worker if
                # isolation cannot be established.  Returning ``None`` lets
                # the worker record a retryable LLM-unavailable run instead
                # of leaking usage/tool/history state across users.
                logger.warning("Dreaming LLM client isolation unavailable")
                return None

        kwargs: dict[str, Any] = {
            "service": None,
            "config": self.config,
            "llm_client_factory": _llm_client_factory,
            "interval_seconds": self._dreaming_poll_interval_seconds(),
        }
        try:
            import inspect

            parameters = inspect.signature(worker_type).parameters
            if not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
            self._dreaming_memory_worker = worker_type(**kwargs)
        except Exception as exc:
            logger.warning("Dreaming Memory worker registration skipped: %s", exc)
            self._dreaming_memory_worker = None
            return
        self._register_lifecycle_pair(
            self._dreaming_memory_worker.start,
            self._dreaming_memory_worker.stop,
        )

        async def _cleanup_owned_dreaming_client() -> None:
            client = owned_background_client.get("client")
            owned_background_client["client"] = None
            if client is None or client is getattr(self, "_llm_client", None):
                return
            for name in ("aclose", "close", "shutdown", "cleanup"):
                hook = getattr(client, name, None)
                if callable(hook):
                    try:
                        result = hook()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.debug("Dreaming owned LLM client cleanup failed", exc_info=True)
                    break

        self._shutdown_background_tasks.append(_cleanup_owned_dreaming_client)

    def _dreaming_poll_interval_seconds(self) -> float:
        """Read Dreaming's idle poll interval with a safe five-minute default."""
        default_interval = 5 * 60.0
        try:
            configured = os.getenv("AOITALK_DREAMING_POLL_INTERVAL_SECONDS")
            if configured:
                value = float(configured)
                return value if value > 0 else default_interval
            if hasattr(self.config, "get"):
                value = self.config.get(
                    "web_interface.memory.dreaming_interval_seconds",
                    default_interval,
                )
            elif isinstance(self.config, dict):
                value = (
                    self.config.get("web_interface", {})
                    .get("memory", {})
                    .get("dreaming_interval_seconds", default_interval)
                )
            else:
                value = default_interval
            value = float(value)
            return value if value > 0 else default_interval
        except Exception:
            return default_interval

    async def _on_startup(self):
        """Startup event handler - ensures admin user exists"""
        agent_work_ready = False
        if not Features.validate_dependencies():
            errors = Features.dependency_errors()
            logger.error("Feature dependency validation failed: %s", errors)
            if Features.is_enterprise():
                raise RuntimeError("invalid Enterprise feature dependency configuration")
        # Build the single durable AgentWork coordinator before Heartbeat or
        # Agent Harness startup hooks run.  Feature gates are checked here and
        # inside the coordinator, so Enterprise/stale config cannot start a
        # hidden queue.  Heartbeat only wakes discovery; long execution is
        # owned by this coordinator.
        if self._db_manager is not None and Features.autonomous_agent_runtime():
            try:
                from ..services.agent_work_runtime import AgentWorkCoordinator
                from ..services.task_work_source import TaskWorkSource
                from ..services.task_execution_adapter import TaskExecutionAdapter
                from ..services.actor_principal import ActorPrincipal

                if self._agent_work_coordinator is None:
                    self._agent_work_coordinator = AgentWorkCoordinator(
                        self._db_manager,
                        config=self.config,
                        max_concurrency=None,
                        task_executor=getattr(self, "_agent_task_executor", None),
                        execution_actor=ActorPrincipal.service("aoitalk.system"),
                        enabled=True,
                    )
                self._agent_work_coordinator.server_owned = True
                if Features.virtual_company():
                    from ..services.ai_employee_platform import register_ai_employee_work

                    register_ai_employee_work(self._agent_work_coordinator, self.ai_employee_services)
                if getattr(self._agent_work_coordinator, "execution_actor", None) is None:
                    self._agent_work_coordinator.execution_actor = ActorPrincipal.service(
                        "aoitalk.system"
                    )
                task_executor = getattr(self, "_agent_task_executor", None)
                # A plain Task WorkSource is only executable through an
                # explicitly injected TaskManagementService callback.  Do not
                # fabricate a human User principal or advertise a mutation
                # lane that would immediately dead-letter every task.
                if (Features.virtual_company() or Features.code_agent()) and callable(task_executor):
                    self._agent_work_coordinator.register_source(TaskWorkSource())
                    self._agent_work_coordinator.register_adapter(
                        TaskExecutionAdapter(task_executor)
                    )
                # Materialize the harness once before recovery/startup.  Its
                # CodeAgentExecutionAdapter is otherwise created lazily on
                # the first API request, which could let a poller claim a
                # code-agent item with no registered adapter.
                harness_router = getattr(self, "_agent_harness_router", None)
                eager_harness = getattr(
                    harness_router,
                    "agent_harness_get_orchestrator",
                    None,
                )
                if Features.code_agent() and callable(eager_harness):
                    try:
                        await eager_harness()
                    except Exception:
                        logger.warning(
                            "Code-agent adapter eager registration failed",
                            exc_info=True,
                        )
                # Gateway delegation joins this exact coordinator before stale
                # recovery; it never starts a second worker or inference loop.
                from ..services.local_agent_tasks import get_local_agent_task_manager, local_agent_tasks_enabled
                from ..services.local_agent_task_adapter import bind_local_agent_task_coordinator

                if local_agent_tasks_enabled(self.config):
                    bind_local_agent_task_coordinator(
                        get_local_agent_task_manager(self.config),
                        self._agent_work_coordinator,
                    )
                # Generic API Agent tasks own an evidence-based restart hook.
                # Without eager binding the generic fallback dead-letters an
                # interrupted max_attempts=1 task before the first request
                # lazily creates the service.
                if self._db_manager is not None:
                    from .agent_task_routes import bind_agent_task_service

                    agent_task_service = bind_agent_task_service(
                        self._db_manager, self.config, self._agent_work_coordinator
                    )
                # The CLI development session facade uses the same durable
                # coordinator and must be attached before stale recovery or
                # the poller can observe its WorkSource.
                await self._attach_cli_development_session_service()
                with _startup_timer.phase("startup.web.lifespan.agent_work_recovery"):
                    await self._agent_work_coordinator.recover_stale()
                    if self._db_manager is not None:
                        try:
                            await agent_task_service.migrate_workspace_fence_keys()
                        except Exception:
                            logger.warning("API Agent task fence migration skipped", exc_info=True)
                    if Features.virtual_company() and Features.voice_input():
                        # Only expired phone sessions beyond their absolute
                        # lifetime are classified; another worker's live call
                        # is never interrupted and no provider command retries.
                        await self.ai_employee_services.telephony.recover_interrupted_calls()
                agent_work_ready = True
            except Exception as exc:
                logger.warning("Common AgentWork coordinator startup skipped: %s", exc)
                if Features.is_enterprise() or os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {"1", "true", "yes", "on"}:
                    raise RuntimeError("Common AgentWork coordinator failed to start") from exc
        # AgentRun provider tasks and human-interaction Futures are process
        # local; they cannot be resumed safely after a restart.  Reconcile any
        # stale running run before dispatch recovery so its terminal audit is
        # visible to callers, while queued outbox rows remain recoverable.
        try:
            from ..services.agent_run_service import AgentRunService

            if self._db_manager is not None:
                with _startup_timer.phase(
                    "startup.web.lifespan.agent_run_reconciliation"
                ):
                    reconciliation = await AgentRunService(
                        self._db_manager
                    ).reconcile_stale_runs_after_restart()
                if reconciliation.get("reconciled") or reconciliation.get("closed_edges"):
                    logger.info(
                        "AgentRun startup reconciliation: runs=%s edges=%s",
                        reconciliation.get("reconciled", 0),
                        reconciliation.get("closed_edges", 0),
                    )
        except Exception as exc:
            # Normal profiles keep startup available if the optional database
            # is down; enterprise/database-required startup will fail later at
            # its existing bootstrap gate.
            logger.warning("AgentRun startup reconciliation skipped: %s", exc)

        # 料金カタログをDBへ同期する（idempotent upsert）。
        # ファイル同期はDBのみで完結するので同期実行し、外部APIを叩く
        # OpenRouter の更新だけは起動をブロックしないよう背後で走らせる。
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            tracking = get_token_tracking_service()
            with _startup_timer.phase("startup.web.lifespan.pricing_sync"):
                sync_result = await tracking.ensure_pricing_catalog(
                    refresh_openrouter=False
                )
            catalog = (sync_result or {}).get("catalog") or {}
            logger.info(
                "料金カタログを同期しました: version=%s inserted=%s updated=%s unchanged=%s",
                catalog.get("catalog_version"),
                catalog.get("inserted"),
                catalog.get("updated"),
                catalog.get("unchanged"),
            )

            async def _refresh_openrouter_pricing() -> None:
                try:
                    from ..services.pricing.updater import refresh_openrouter_catalog

                    result = await refresh_openrouter_catalog()
                    logger.info("OpenRouter料金表の更新: %s", result.get("status"))
                except Exception as exc:
                    logger.warning("OpenRouter料金表の更新に失敗しました: %s", exc)

            self._spawn_lifespan_task(
                _refresh_openrouter_pricing(),
                name="aoitalk-web-startup:refresh-openrouter-pricing",
            )
        except Exception as exc:
            logger.error(f"Failed to sync pricing catalog: {exc}")

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            logger.info("Admin initialization skipped: UserRepository not available")
            return

        require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
            "1", "true", "yes", "on"
        } or Features.is_enterprise()

        try:
            session = await self._db_manager.get_session()
            try:
                with _startup_timer.phase("startup.web.lifespan.admin_bootstrap"):
                    admin_created = await UserRepository.ensure_admin_exists(session)
                if admin_created:
                    logger.warning(
                        "初期管理者を作成しました。"
                        "AOITALK_BOOTSTRAP_ADMIN_PASSWORD でログインし、"
                        "必ずパスワードを変更してください。"
                    )
                else:
                    logger.info("Admin user already exists")

                if Features.is_enterprise():
                    from ..memory.enterprise_bootstrap_repository import (
                        EnterpriseBootstrapRepository,
                    )

                    bootstrap_username = (
                        os.getenv("AOITALK_BOOTSTRAP_ADMIN_USERNAME", "admin").strip()
                        or "admin"
                    )
                    with _startup_timer.phase(
                        "startup.web.lifespan.enterprise_bootstrap"
                    ):
                        await EnterpriseBootstrapRepository.initialize(
                            session,
                            configured_username=bootstrap_username,
                        )
                        await session.commit()

                # WS01 deployment-wide Organization bootstrap.  This is an
                # additive singleton and does not add tenancy columns to
                # existing Space/Project/Task rows.  The service's fixed-key
                # uniqueness and transaction-safe replay handle concurrent
                # startup processes.
                try:
                    from ..services.agent_identity_service import OrganizationService

                    with _startup_timer.phase(
                        "startup.web.lifespan.organization_bootstrap"
                    ):
                        await OrganizationService(self._db_manager).bootstrap()
                    logger.info("Organization singleton bootstrap complete")
                except Exception as organization_error:
                    logger.exception(
                        "Failed to bootstrap Organization singleton: %s",
                        organization_error,
                    )
                    if require_database:
                        raise RuntimeError(
                            "Organization singleton bootstrap failed; refusing to start"
                        ) from organization_error

            except Exception as e:
                logger.exception(f"Failed to ensure admin exists: {e}")
                if require_database:
                    raise RuntimeError(
                        "Enterprise admin bootstrap failed; refusing to start"
                    ) from e
            finally:
                await session.close()
        except Exception as e:
            logger.exception(
                f"Failed to get database session for admin initialization: {e}"
            )
            if require_database:
                raise RuntimeError(
                    "Enterprise admin bootstrap could not obtain a database session"
                ) from e

        # The Guide is a canonical, user-owned Docs subtree rather than a
        # lazy first-request side effect.  Backfill every existing account in
        # isolated transactions before notification workers can dispatch a
        # request.  Required-database profiles fail closed if any account
        # cannot be repaired; optional profiles remain available and retry on
        # the next startup while retaining the per-user error log above.
        try:
            from ..services.aoitalk_guide import backfill_aoitalk_guides

            with _startup_timer.phase("startup.web.lifespan.aoitalk_guide_backfill"):
                guide_backfill = await backfill_aoitalk_guides(self._db_manager)
            guide_failures = int((guide_backfill or {}).get("failed", 0) or 0)
            if guide_failures:
                message = (
                    "AoiTalk Guide backfill failed for "
                    f"{guide_failures}/{int((guide_backfill or {}).get('users', 0) or 0)} users"
                )
                if require_database:
                    raise RuntimeError(message)
                logger.warning(message)
            else:
                logger.info(
                    "AoiTalk Guide backfill complete: users=%s ensured=%s",
                    (guide_backfill or {}).get("users", 0),
                    (guide_backfill or {}).get("ensured", 0),
                )
        except Exception as exc:
            logger.exception("AoiTalk Guide backfill failed: %s", exc)
            if require_database:
                raise RuntimeError(
                    "Enterprise AoiTalk Guide backfill failed; refusing to start"
                ) from exc

        if self._task_notification_worker:
            try:
                with _startup_timer.phase("startup.web.lifespan.notification_worker_sync"):
                    await self._task_notification_worker.run_once()
            except Exception as exc:
                logger.error(f"Task startup sync failed: {exc}")

        # Keep optional workers from opening a burst of new PostgreSQL
        # connections while the required recovery and bootstrap steps are
        # still waiting for their own connection.  On Windows that burst can
        # stall the AgentRun reconciliation until FastAPI's readiness timeout.
        pending_background_tasks = list(self._startup_background_tasks)
        with _startup_timer.phase("startup.web.lifespan.background_schedule"):
            for task_factory in pending_background_tasks:
                try:
                    startup_coro = task_factory()
                    if not inspect.isawaitable(startup_coro):
                        raise TypeError(
                            f"startup hook {task_factory!r} did not return an awaitable"
                        )
                    task_name = str(
                        getattr(task_factory, "__name__", "startup") or "startup"
                    )
                    task = self._spawn_lifespan_task(
                        startup_coro,
                        name=f"aoitalk-web-startup:{task_name}",
                        startup_factory=task_factory,
                    )
                    if task_factory is self._mage_vl_preload_factory:
                        self._mage_vl_preload_task = task
                except Exception as exc:
                    logger.error(f"Failed to schedule startup background task: {exc}")

        # All startup hooks (including Agent Harness adapter binding) have now
        # had a scheduling turn.  Start the one common coordinator last so no
        # work can be claimed before its adapters are registered.
        coordinator = getattr(self, "_agent_work_coordinator", None)
        if coordinator is not None and Features.autonomous_agent_runtime() and agent_work_ready:
            if Features.media_operations_autonomy():
                try:
                    await self._register_media_agent_work_lanes(coordinator)
                except Exception as exc:
                    logger.warning("Media AgentWork lane registration skipped: %s", exc)
            await coordinator.start(
                poll_interval_seconds=self._agent_work_poll_interval_seconds()
            )
            self._agent_work_coordinator_started = True

    async def _register_media_agent_work_lanes(self, coordinator: Any) -> None:
        """Register MediaOps WorkSources/adapters on the shared coordinator."""

        if not Features.media_operations_autonomy():
            return
        session = await self._db_manager.get_session()
        try:
            from ..memory.models import User
            admin = (
                await session.execute(
                    select(User)
                    .where(User.is_active.is_(True), User.role == "admin")
                    .order_by(User.created_at)
                    .limit(1)
                )
            ).scalars().first()
            if admin is None:
                return
            # Legacy MediaOps services use owner_user_id for ACL joins.  This
            # is a server-owned human context projection; the true Agent ID
            # remains in WorkItem/AgentRun/origin fields and is never written
            # into a User FK.
            actor = {
                "id": str(admin.id),
                "user_id": str(admin.id),
                "actor_type": "human",
                "is_agent": False,
                "role": "admin",
                "_autonomous_discovery": True,
            }
            from ..services.agent_authority import AgentAuthorityResolver
            from ..services.media_work_sources import register_media_work_sources
            from ..services.media_execution_adapters import register_media_execution_adapters
            from ..services.media_operations_research_service import MediaOperationsResearchService
            from ..services.media_operations_generation_service import MediaOperationsGenerationService
            from ..services.media_operations_content_service import MediaOperationsContentService
            from ..services.media_operations_metrics_service import MediaOperationsMetricsService
            from ..services.media_operations_learning_service import MediaOperationsLearningService
            from ..services.operations_service import create_operations_service

            resolver = AgentAuthorityResolver(self._db_manager, config=self.config)
            async def media_actor_resolver(work_item: Any = None, *, session: Any = None, **_: Any) -> Any:
                """Resolve the owning human context for one Media source.

                Legacy Media ledgers retain ``owner_user_id``.  The resolver
                uses that explicit owner for ACL joins while Agent identity
                and origin remain in the common WorkItem/AgentRun rows.
                """
                if session is None:
                    return None
                raw = work_item if isinstance(work_item, dict) else vars(work_item) if work_item is not None else {}
                persona_id = raw.get("persona_id")
                if persona_id is None and isinstance(raw.get("metadata"), dict):
                    payload = raw["metadata"].get("payload")
                    if isinstance(payload, dict):
                        persona_id = payload.get("persona_id")
                if persona_id is None:
                    return None
                try:
                    from ..memory.models import Persona
                    persona = await session.get(Persona, persona_id)
                except Exception:
                    return None
                owner_id = getattr(persona, "owner_user_id", None) if persona is not None else None
                if owner_id is None:
                    return None
                try:
                    owner = await session.get(User, owner_id)
                except Exception:
                    return None
                if owner is None or getattr(owner, "is_active", True) is False:
                    return None
                return {"id": str(owner_id), "user_id": str(owner_id), "actor_type": "human", "is_agent": False}
            kwargs = {
                "authority_resolver": resolver.resolve,
                "actor": actor,
                "feature_checker": Features.media_operations_autonomy,
                "research_service": MediaOperationsResearchService(),
                "generation_service": MediaOperationsGenerationService(),
                "publication_service": MediaOperationsContentService(),
                "metrics_service": MediaOperationsMetricsService(),
                "learning_service": MediaOperationsLearningService(),
            }
            register_media_work_sources(coordinator, **kwargs)
            register_media_execution_adapters(
                coordinator,
                research_service=kwargs["research_service"],
                generation_service=kwargs["generation_service"],
                operations_service=create_operations_service(),
                metrics_service=kwargs["metrics_service"],
                learning_service=kwargs["learning_service"],
                authority_resolver=resolver.resolve,
                actor_resolver=media_actor_resolver,
                config=self.config,
                feature_checker=Features.media_operations_autonomy,
            )
        finally:
            await session.close()

    def _setup_routes(self):
        """Setup API routes (ドメイン別の registrar モジュールへ委譲)"""
        register_system_routes(self.app, self)
        from .routes.embedding_runtime_routes import register_embedding_runtime_routes

        register_embedding_runtime_routes(self.app, self)
        from .routes.gpu_admission_routes import register_gpu_admission_routes

        register_gpu_admission_routes(self.app, self)
        register_config_routes(self.app, self)
        from .routes.pc_bridge_routes import register_pc_bridge_routes
        register_pc_bridge_routes(self.app, self)
        register_chatgpt_web_routes(self.app, self)
        register_yomi_linter_routes(self.app, self)
        register_llm_routes(self.app, self)
        register_free_team_routes(self.app, self)
        if Features.crawler_status():
            register_crawler_routes(self.app, self)
        if not Features.is_enterprise():
            register_mobile_command_routes(self.app, self)
        register_conversation_dispatch_routes(self.app, self)
        from .media_search_routes import create_media_search_router
        self.app.include_router(create_media_search_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=cookie_auth_dependency(self._enforce_cookie_auth),
            workspace_root=self._resolve_workspace_root(),
            queue_provider=lambda: self._media_index_worker.queue,
        ))
        from .files_index_routes import create_files_index_router
        self.app.include_router(create_files_index_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=cookie_auth_dependency(self._enforce_cookie_auth),
            queue_provider=lambda: self._media_index_worker.queue,
        ))
        register_agent_run_routes(self.app, self)
        if AGENT_WORK_ROUTES_AVAILABLE and create_agent_work_router:
            self._register_agent_work_routes()
        self._register_agent_task_routes()
        if AGENT_IDENTITY_ROUTES_AVAILABLE and create_agent_identity_router:
            self._register_agent_identity_routes()
        from .agent_dialogue_routes import create_agent_dialogue_router
        from ..services.agent_dialogue_service import get_agent_dialogue_dispatch_bridge
        dialogue_dispatch_bridge = get_agent_dialogue_dispatch_bridge(self._db_manager, config=self.config)
        self._agent_dialogue_dispatch_bridge = dialogue_dispatch_bridge
        self.app.include_router(create_agent_dialogue_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=cookie_auth_dependency(self._enforce_cookie_auth),
            get_dispatch_bridge=lambda: dialogue_dispatch_bridge,
            config=self.config,
        ))
        register_live_voice_routes(self.app, self)
        register_voice_session_routes(self.app, self)
        from .routes.mobile_voice_routes import register_mobile_voice_routes

        register_mobile_voice_routes(self.app, self)
        from .routes.tts_provider_routes import register_tts_provider_routes

        register_tts_provider_routes(self.app, self)
        from .ai_employee_registration import register_ai_employee_routes

        register_ai_employee_routes(self, cookie_auth_dependency(self._enforce_cookie_auth))
        register_file_explorer_routes(self.app, self)
        register_ogp_routes(self.app, self)
        register_document_storage_routes(self.app, self)
        register_auth_routes(self.app, self)
        register_api_token_routes(self.app, self)
        from .routes.personal_ledger_routes import register_personal_ledger_routes

        register_personal_ledger_routes(self.app, self)
        from .routes.personal_draft_routes import register_personal_draft_routes

        register_personal_draft_routes(self.app, self)
        from .routes.personal_finance_routes import register_personal_finance_routes

        register_personal_finance_routes(self.app, self)
        from .routes.personal_career_routes import register_personal_career_routes

        register_personal_career_routes(self.app, self)
        from .routes.personal_agent_routes import register_personal_agent_routes

        register_personal_agent_routes(self.app, self)
        from ..services.personal_reminder_service import PersonalReminderService
        from ..services.personal_reminder_worker import PersonalReminderWorker
        from .routes.personal_reminder_routes import register_personal_reminder_routes

        reminder_service = PersonalReminderService(
            lambda: self._db_manager.get_session()
        )
        reminder_worker = PersonalReminderWorker(
            reminder_service,
            config=self.config,
        )
        self._personal_reminder_service = reminder_service
        self._personal_reminder_worker = reminder_worker
        # This worker only delivers schedules explicitly approved by the owner.
        # It is disabled unless an operator deliberately enables the narrow
        # personal_reminders.worker_enabled setting after staging migration 0105.
        self._register_lifecycle_pair(reminder_worker.start, reminder_worker.stop)
        register_personal_reminder_routes(
            self.app,
            self,
            service=reminder_service,
            schema_ready=reminder_service.schema_available,
            worker_ready=lambda: reminder_worker.ready,
        )
        register_capabilities_routes(self.app, self)
        if not Features.is_enterprise():
            register_remote_server_routes(self.app, self)
            register_remote_proxy_routes(self.app, self)
        register_user_admin_routes(self.app, self)
        register_verification_data_routes(self.app, self)
        register_feedback_routes(self.app, self)
        register_websocket_routes(self.app, self)

    def _register_agent_identity_routes(self):
        """Register the additive WS01 Agent identity/authority API."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_agent_identity_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            config=self.config,
        )
        self.app.include_router(router)
        logger.info("Generic Agent identity routes registered")

    def _register_agent_work_routes(self):
        """Register the common durable AgentWork read/maintenance API."""

        if not AGENT_WORK_ROUTES_AVAILABLE or not create_agent_work_router:
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_agent_work_router(
            get_db_manager=lambda: self._db_manager,
            require_auth_dependency=require_auth,
            is_admin_user=self._is_admin_user,
            get_coordinator=lambda: self._agent_work_coordinator,
            get_user_from_request=self._get_user_info_from_request,
            get_generation_status=self.get_conversation_generation_status,
        )
        self.app.include_router(router)
        logger.info("Common AgentWork routes registered")

    def _register_agent_task_routes(self):
        """Register authenticated invocation routes for generic API Agents."""

        from .agent_task_routes import create_agent_task_router

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        self.app.include_router(
            create_agent_task_router(
                get_db_manager=lambda: self._db_manager,
                get_coordinator=lambda: self._agent_work_coordinator,
                get_user_from_request=self._get_user_info_from_request,
                require_auth_dependency=require_auth,
                config=self.config,
            )
        )
        from .agent_task_listing_routes import create_agent_task_listing_router

        self.app.include_router(
            create_agent_task_listing_router(
                get_db_manager=lambda: self._db_manager,
                get_user_from_request=self._get_user_info_from_request,
                require_auth_dependency=require_auth,
            )
        )
        logger.info("Generic Agent task routes registered")

    def _agent_work_poll_interval_seconds(self) -> float:
        """Return a bounded coordinator poll interval from app config."""

        default = 5.0
        try:
            raw = os.getenv("AOITALK_AGENT_WORK_POLL_INTERVAL_SECONDS")
            if raw:
                return max(0.05, min(float(raw), 3600.0))
            if hasattr(self.config, "get"):
                raw = self.config.get("agent_work.poll_interval_seconds", default)
            elif isinstance(self.config, dict):
                raw = self.config.get("agent_work", {}).get("poll_interval_seconds", default)
            else:
                raw = default
            return max(0.05, min(float(raw), 3600.0))
        except Exception:
            return default

    def _build_cors_origins(self) -> List[str]:
        """CORS の許可オリジン一覧を組み立てる。

        - 環境変数 AOITALK_CORS_ORIGINS（カンマ区切り）があれば最優先で使用する。
        - なければローカル開発用デフォルトに、config の公開URL設定
          （web_interface.public_url）があれば追加する。
        """
        env_origins = os.environ.get("AOITALK_CORS_ORIGINS", "")
        if env_origins.strip():
            origins: List[str] = []
            for raw_origin in env_origins.split(","):
                origin = _normalize_cors_origin(raw_origin)
                if origin is None:
                    if raw_origin.strip():
                        logger.warning("安全でないCORS origin設定を無視しました")
                    continue
                if origin not in origins:
                    origins.append(origin)
            return origins

        origins = ["http://127.0.0.1:3002", "http://localhost:3002"]
        try:
            public_url = None
            if isinstance(self.config, dict):
                public_url = self.config.get("web_interface.public_url")
                if public_url is None:
                    public_url = (
                        self.config.get("web_interface", {}) or {}
                    ).get("public_url")
            elif hasattr(self.config, "get"):
                public_url = self.config.get("web_interface.public_url", None)
            if isinstance(public_url, str) and public_url.strip():
                origin = _normalize_cors_origin(public_url)
                if origin is None:
                    logger.warning("安全でない公開URLのCORS originを無視しました")
                elif origin not in origins:
                    origins.append(origin)
        except Exception as exc:
            logger.warning(f"公開URL設定の読み込みに失敗しました: {exc}")
        return origins

    def _extract_task_notification_poll_interval(self) -> int:
        """Read task reminder polling interval from config."""
        default_interval = 60
        try:
            if hasattr(self.config, "get"):
                value = self.config.get(
                    "web_interface.tasks.notification_poll_interval_seconds",
                    default_interval,
                )
            elif isinstance(self.config, dict):
                value = (
                    self.config.get("web_interface", {})
                    .get("tasks", {})
                    .get("notification_poll_interval_seconds", default_interval)
                )
            else:
                value = default_interval
            value = int(value)
            return value if value > 0 else default_interval
        except Exception:
            return default_interval

    def _register_frontend_catchall(self):
        @self.app.get("/{frontend_path:path}")
        async def get_frontend_path(frontend_path: str):
            """Redirect non-API requests to Next.js frontend."""
            if frontend_path.startswith(("api", "ws")):
                raise HTTPException(status_code=404, detail="Not found")

            from starlette.responses import RedirectResponse

            target = (
                f"{self._nextjs_url}/{frontend_path}"
                if frontend_path
                else self._nextjs_url
            )
            return RedirectResponse(url=target)
    def _resolve_workspace_root(self):
        """App/Project workspace の実効 root をプロセス内で 1 度だけ確定する。

        App の排他制御は lock file の path で決まるため、ロックを取る側と
        実ファイルを触る側で root がずれると排他が静かに壊れる。router 生成時に
        解決済みの絶対 path を 1 つ作り、Apps / Project / Sync のすべてへ同じ値を
        配ることで、cwd 変更や env の読み直しで root が分裂しないようにする。
        """
        cached = getattr(self, "_workspace_root_cache", None)
        if cached is None:
            from ..services.app_storage import get_workspaces_root

            cached = get_workspaces_root()
            self._workspace_root_cache = cached
        return cached

    def _register_project_routes(self):
        """Register project API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_project_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            workspace_root=self._resolve_workspace_root(),
        )
        self.app.include_router(router)
        logger.info("Project routes registered")

    def _register_project_overview_routes(self):
        """Register Project Overview routes."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_project_overview_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            get_config=self._load_project_overview_config_snapshot,
        )
        self.app.include_router(router)
        logger.info("Project Overview routes registered")

    def _register_project_docs_candidate_routes(self):
        """Register the Project Docs candidate review queue routes."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_project_docs_candidate_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Project Docs candidate routes registered")

    def _register_knowledge_capture_routes(self):
        """Register authenticated Resolution Knowledge Capture routes."""

        if not KNOWLEDGE_CAPTURE_ROUTES_AVAILABLE or not create_knowledge_capture_router:
            logger.warning("Knowledge Capture routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_knowledge_capture_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            config=self.config,
        )
        self.app.include_router(router)
        logger.info("Knowledge Capture routes registered")

    def _register_operations_routes(self):
        """Register the authenticated Engagement Operations API."""

        if not OPERATIONS_ROUTES_AVAILABLE or not create_operations_router:
            logger.warning("Operations routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_operations_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            action_registry=self.ai_employee_services.registry,
        )
        self.app.include_router(router)
        logger.info("Engagement Operations routes registered")

    def _register_media_operations_routes(self):
        """Register the authenticated typed Media Operations API."""

        if (
            not MEDIA_OPERATIONS_ROUTES_AVAILABLE
            or not create_media_operations_router
        ):
            logger.warning("Media Operations routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations routes registered")

    def _register_media_operations_setup_routes(self):
        """Register MediaOps bulk setup and PlatformAccount API."""

        if (
            not MEDIA_OPERATIONS_SETUP_ROUTES_AVAILABLE
            or not create_media_operations_setup_router
        ):
            logger.warning(
                "Media Operations setup routes not available"
            )
            return

        require_auth = cookie_auth_dependency(
            self._enforce_cookie_auth
        )
        router = create_media_operations_setup_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info(
            "Media Operations setup routes registered"
        )

    def _register_media_operations_research_routes(self):
        """Register MediaOps research evidence and editorial trace API."""

        if (
            not MEDIA_OPERATIONS_RESEARCH_ROUTES_AVAILABLE
            or not create_media_operations_research_router
        ):
            logger.warning(
                "Media Operations research routes not available"
            )
            return

        require_auth = cookie_auth_dependency(
            self._enforce_cookie_auth
        )
        router = create_media_operations_research_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info(
            "Media Operations research routes registered"
        )

    def _register_media_operations_generation_routes(self):
        """Register the authenticated Generation Studio provenance API."""

        if (
            not MEDIA_OPERATIONS_GENERATION_ROUTES_AVAILABLE
            or not create_media_operations_generation_router
        ):
            logger.warning(
                "Media Operations generation routes not available"
            )
            return

        require_auth = cookie_auth_dependency(
            self._enforce_cookie_auth
        )
        router = create_media_operations_generation_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info(
            "Media Operations generation routes registered"
        )

    def _register_media_operations_automation_routes(self):
        """Register AoiTalk-owned MediaOps Automation API."""
        if (
            not MEDIA_OPERATIONS_AUTOMATION_ROUTES_AVAILABLE
            or not create_media_operations_automation_router
        ):
            logger.warning("Media Operations automation routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_automation_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations automation routes registered")

    def _register_media_operations_content_routes(self):
        """Register the authenticated ContentVariant/QA/Rights API."""
        if (
            not MEDIA_OPERATIONS_CONTENT_ROUTES_AVAILABLE
            or not create_media_operations_content_router
        ):
            logger.warning("Media Operations content routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_content_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations content routes registered")

    def _register_media_operations_metrics_routes(self):
        """Register the authenticated metrics/experiments/learning API."""
        if (
            not MEDIA_OPERATIONS_METRICS_ROUTES_AVAILABLE
            or not create_media_operations_metrics_router
        ):
            logger.warning("Media Operations metrics routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_metrics_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations metrics routes registered")

    def _register_media_operations_learning_routes(self):
        """Register human-only Learning review/apply commands."""
        if (
            not MEDIA_OPERATIONS_LEARNING_ROUTES_AVAILABLE
            or not create_media_operations_learning_router
        ):
            logger.warning("Media Operations learning routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_learning_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations learning routes registered")

    def _register_media_operations_overview_routes(self):
        """Register the read-only MediaOps Calendar/Results projections."""
        if (
            not MEDIA_OPERATIONS_OVERVIEW_ROUTES_AVAILABLE
            or not create_media_operations_overview_router
        ):
            logger.warning("Media Operations overview routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_media_operations_overview_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Media Operations overview routes registered")

    def _register_knowledge_routes(self):
        """Register Knowledge Workspace routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_knowledge_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Knowledge Workspace routes registered")

    def _register_deep_research_routes(self):
        """Register local Deep Research API routes."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_deep_research_router(
            require_auth_dependency=require_auth,
            get_current_user=self._get_user_info_from_request,
            config=self.config if hasattr(self, "config") else {},
        )
        self.app.include_router(router)
        # The Deep Research router owns a bounded worker pool.  Register its
        # shutdown hook with the composition root as well as the router event
        # so WebChatServer's custom lifespan always drains/cancels workers,
        # including runtimes that bypass FastAPI's deprecated router events.
        manager = getattr(router, "deep_research_manager", None)
        reopen = getattr(manager, "reopen", None)
        if callable(reopen):
            async def _reopen_deep_research_manager() -> None:
                reopen()

            # The composition root uses a custom lifespan and keeps startup
            # hooks as reusable templates.  Re-arm the manager here as well
            # as on the router event so a second lifespan cannot inherit the
            # previous shutdown's closed flag.
            self._startup_background_tasks.append(_reopen_deep_research_manager)
        shutdown = getattr(manager, "shutdown", None)
        if callable(shutdown):
            self._shutdown_background_tasks.append(shutdown)
        logger.info("Deep Research routes registered")

    def _register_conversation_routes(self):
        """Register Conversation History API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        async def generate_title_via_llm(prompt: str) -> Optional[str]:
            """Generate title using the already-running main LLM client."""
            return await generate_title_with_llm_client(self._llm_client, prompt)

        router = create_conversation_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
            get_llm_for_title_generation=generate_title_via_llm,
            get_title_config=lambda: self.config,
        )
        self.app.include_router(router)
        if CONVERSATION_ROUTES_AVAILABLE and create_conversation_agent_router:
            self.app.include_router(
                create_conversation_agent_router(
                    require_auth=require_auth,
                    get_current_user=self._get_user_info_from_request,
                    config=self.config,
                )
            )
        logger.info("Conversation routes registered")

    def _register_chat_intent_routes(self):
        """Register optional, non-persistent pre-send intent suggestions."""
        from .chat_intent_routes import create_chat_intent_router
        from ..services.chat_intent_service import CHAT_INTENT_TIMEOUT_SECONDS
        from ..services.jev_decision_service import JevClientPool

        client_pool = JevClientPool(timeout_seconds=CHAT_INTENT_TIMEOUT_SECONDS)
        self._register_lifecycle_pair(client_pool.start, client_pool.aclose)
        self.app.include_router(create_chat_intent_router(
            require_auth_dependency=cookie_auth_dependency(self._enforce_cookie_auth),
            get_current_user=self._get_user_info_from_request,
            config=self.config,
            client_pool=client_pool,
        ))

    def _register_group_chat_routes(self):
        """Register Group Chat API routes"""
        if not GROUP_CHAT_ROUTES_AVAILABLE:
            logger.warning("Group chat routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_group_chat_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
            config=self.config if hasattr(self, "config") else None,
            masking_handler=self._execute_builtin_masking_turn,
        )
        self.app.include_router(router)
        logger.info("Group chat routes registered")

    def _register_skill_routes(self):
        """Register Skills API routes"""
        if not SKILL_ROUTES_AVAILABLE:
            logger.warning("Skill routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_skill_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
        )
        self.app.include_router(router)
        logger.info("Skill routes registered")

    def _register_skill_recording_routes(self):
        """Register Skill Recording API routes"""
        if not SKILL_RECORDING_ROUTES_AVAILABLE:
            logger.warning("Skill recording routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_skill_recording_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
            config=self.config if hasattr(self, "config") else None,
        )
        self.app.include_router(router)
        logger.info("Skill recording routes registered")

    def _register_task_routes(self):
        """Register task management API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_task_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            broadcaster=self.manager.broadcast,
            workspace_root=self._resolve_workspace_root(),
            config=getattr(self, "config", None),
        )
        self.app.include_router(router)
        logger.info("Task management routes registered")

    def _register_webex_routes(self):
        """Register Webex Messaging integration routes."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_webex_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            config=getattr(self, "config", None),
        )
        self.app.include_router(router)
        logger.info("Webex Messaging routes registered")

    def _register_sync_routes(self):
        """Register mobile sync API routes"""

        if Features.is_enterprise():
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_sync_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            workspace_root=self._resolve_workspace_root(),
        )
        self.app.include_router(router)
        logger.info("Mobile sync routes registered")

        from ..sync_v3.change_feed import PgChangeFeed
        from ..sync_v3.settings import sync_v3_enabled
        from .sync_v3_ops_routes import create_sync_v3_ops_router
        from .sync_v3_routes import create_sync_v3_router

        self.app.include_router(
            create_sync_v3_router(
                PgChangeFeed(),
                get_db_manager=lambda: self._db_manager,
                get_user_from_request=self._get_user_info_from_request,
                require_auth_dependency=require_auth,
            )
        )
        self.app.include_router(
            create_sync_v3_ops_router(
                sync_v3_enabled,
                get_db_manager=lambda: self._db_manager,
                get_user_from_request=self._get_user_info_from_request,
                require_auth_dependency=require_auth,
                workspace_root=self._resolve_workspace_root(),
            )
        )
        logger.info("Mobile sync v3 routes registered")

    def _register_docs_routes(self):
        """Register Docs REST API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        @asynccontextmanager
        async def docs_ingest_plan_llm_session(
            *,
            user_id: str | None = None,
            session_id: str | None = None,
            project_id: str | None = None,
        ):
            from ..services.docs_ingest_service import (
                cleanup_ingest_llm_client,
                DocsIngestUnavailableError,
                generate_docs_ingest_plan_text,
                resolve_clip_ingest_llm_client,
                resolved_clip_ingest_route,
            )

            # クリップ取り込み枠に専用モデルが指定されていればそれを使う。
            default_client = self._llm_client
            client = default_client
            try:
                client = resolve_clip_ingest_llm_client(
                    self.config,
                    default_client,
                    user_id=user_id,
                    session_id=session_id,
                    project_id=project_id,
                )
            except DocsIngestUnavailableError:
                # A configured dedicated route must fail closed; falling back
                # here would claim the dedicated provider/model while calling
                # the main endpoint.  Explicit main fallback is handled by
                # ``resolve_clip_ingest_llm_client`` and marks the actual
                # request-scoped client for route recomputation below.
                raise
            except Exception as exc:
                raise DocsIngestUnavailableError(
                    f"クリップ取り込み用LLMの解決に失敗しました: {exc}"
                ) from exc
            # An inherited route is represented by a shallow request-scoped
            # view; it must not invoke cleanup on the process-wide main client.
            owns_client = client is not default_client and not bool(
                getattr(client, "_aoitalk_shared_ingest_client", False)
            )
            try:
                async def plan_llm(prompt: str) -> str:
                    return await generate_docs_ingest_plan_text(client, prompt)

                # Expose the request-scoped resolved route/client to the Docs
                # ingest workflow.  This is metadata on the ephemeral closure,
                # not a mutation of global ``vision`` settings or the shared
                # main client.
                plan_llm.clip_ingest_route = resolved_clip_ingest_route(
                    self.config,
                    client,
                )
                plan_llm.clip_ingest_client = client

                yield plan_llm
            finally:
                # 専用clientは1 HTTP取り込み内だけで共有し、利用者間では共有しない。
                if owns_client:
                    await cleanup_ingest_llm_client(client)

        router = create_docs_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            docs_ingest_plan_llm_factory=docs_ingest_plan_llm_session,
            docs_ingest_config=self.config,
            workspace_root=self._resolve_workspace_root(),
        )
        self.app.include_router(router)

        # Durable ClipIngest jobs are optional during rolling deployments.  Keep
        # this import local so an unavailable worker (or one of its optional
        # dependencies) does not prevent the synchronous Docs routes from
        # registering.
        try:
            from ..services.docs_clip_ingest_worker import DocsClipIngestWorker
        except ImportError as exc:
            logger.warning(
                "Docs ClipIngest worker is unavailable; durable ingest jobs will not run: %s",
                exc,
            )
        else:
            worker = getattr(self, "_docs_clip_ingest_worker", None)
            if worker is None:
                worker = DocsClipIngestWorker(
                    get_db_manager=lambda: self._db_manager,
                    plan_llm_factory=docs_ingest_plan_llm_session,
                    config=self.config,
                    workspace_root=self._resolve_workspace_root(),
                )
                self._docs_clip_ingest_worker = worker
                self._register_lifecycle_pair(worker.start, worker.stop)

        from ..services.daily_capture_worker import DailyCaptureWorker
        daily_worker = DailyCaptureWorker(
            get_db_manager=lambda: self._db_manager,
            plan_llm_factory=docs_ingest_plan_llm_session,
        )
        self._daily_capture_worker = daily_worker
        self._register_lifecycle_pair(daily_worker.start, daily_worker.stop)

        logger.info("Docs routes registered")

    def _register_x_cookie_routes(self):
        """Register the isolated per-user X Cookie management API."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_x_cookie_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Per-user X Cookie routes registered")

    def _register_heartbeat_routes(self):
        """Register Heartbeat API routes"""
        if not HEARTBEAT_ROUTES_AVAILABLE:
            logger.warning("Heartbeat routes not available")
            return

        async def require_admin(request: Request) -> None:
            self._enforce_cookie_auth(request)
            if not await self._is_admin_user(request):
                raise HTTPException(
                    status_code=403, detail="Administrator privileges required"
                )

        router = create_heartbeat_router(require_admin=require_admin)
        self.app.include_router(router)
        logger.info("Heartbeat routes registered")

    def _register_local_agent_task_routes(self):
        """Register admin-only local-LLM task delegation (Home MCP Gateway)."""
        from .local_agent_task_routes import create_local_agent_task_router
        from ..services.local_agent_tasks import (
            get_local_agent_task_manager,
            local_agent_tasks_enabled,
        )
        from ..services.codex_task_preflight import CodexManagedConfigPreflight

        config = self.config if hasattr(self, "config") else {}
        manager = None
        if local_agent_tasks_enabled(config):
            try:
                # The callback only reads managed requirements from an
                # already-running official daemon.  It never starts a daemon
                # or edits the user's Codex configuration/authentication.
                manager = get_local_agent_task_manager(
                    config,
                    codex_preflight=CodexManagedConfigPreflight(),
                )
            except Exception as exc:
                logger.warning("Local agent task manager unavailable: %s", type(exc).__name__)

        async def require_admin(request: Request) -> None:
            self._enforce_cookie_auth(request)
            if not await self._is_admin_user(request):
                raise HTTPException(
                    status_code=403, detail="Administrator privileges required"
                )

        self.app.include_router(create_local_agent_task_router(
            require_admin=require_admin,
            get_manager=lambda: manager,
            get_user_id=self._get_user_info_from_request,
        ))
        if manager is not None:
            async def _start_local_agent_tasks() -> None:
                manager.start()

            async def _stop_local_agent_tasks() -> None:
                manager.stop()

            self._register_lifecycle_pair(_start_local_agent_tasks, _stop_local_agent_tasks)
        logger.info("Local agent task routes registered (enabled=%s)", manager is not None)

    def _register_cli_development_session_routes(self) -> None:
        """Register the authenticated CLI development session facade.

        The optional service is deliberately resolved lazily.  A disabled or
        rolling deployment still exposes the status route, while create/send
        endpoints fail closed until startup has attached the service to the
        shared AgentWork coordinator.
        """

        try:
            from .cli_development_session_routes import (
                create_cli_development_session_router,
            )
        except Exception as exc:
            logger.warning(
                "CLI development session routes unavailable: %s",
                type(exc).__name__,
            )
            return

        async def require_admin(request: Request) -> None:
            self._enforce_cookie_auth(request)
            if not await self._is_admin_user(request):
                raise HTTPException(
                    status_code=403,
                    detail="Administrator privileges required",
                )

        self.app.include_router(
            create_cli_development_session_router(
                require_admin=require_admin,
                get_user_from_request=self._get_user_info_from_request,
                get_service=lambda: self._cli_development_session_service,
                get_config=lambda: self.config,
            )
        )
        logger.info("CLI development session routes registered")

    def _build_cli_development_session_service(self) -> Any | None:
        """Construct the optional session service at the composition root."""

        if self._cli_development_session_service is not None:
            return self._cli_development_session_service
        enabled = False
        try:
            raw = self.config.get("cli_development_sessions.enabled", False)
            if isinstance(self.config, Mapping) and raw is False:
                section = self.config.get("cli_development_sessions", {})
                raw = section.get("enabled", False) if isinstance(section, Mapping) else False
            enabled = raw is True or str(raw).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        except Exception:
            enabled = False
        if not enabled:
            return None
        try:
            from ..services.cli_development_sessions import CLIDevelopmentSessionService
        except Exception as exc:
            logger.warning(
                "CLI development session service import skipped: %s",
                type(exc).__name__,
            )
            return None
        try:
            instance = CLIDevelopmentSessionService(
                self._db_manager,
                config=self.config,
                coordinator=self._agent_work_coordinator,
            )
            self._cli_development_session_service = instance
            return instance
        except Exception as exc:
            logger.warning(
                "CLI development session service construction skipped: %s",
                type(exc).__name__,
            )
            return None

    async def _attach_cli_development_session_service(self) -> None:
        """Attach the session source/adapter to the shared coordinator once."""

        coordinator = self._agent_work_coordinator
        if coordinator is None:
            return
        service = self._build_cli_development_session_service()
        if service is None:
            return
        if self._cli_development_session_work_coordinator is coordinator:
            return
        service.bind_coordinator(coordinator)
        self._cli_development_session_work_coordinator = coordinator

    def _register_agent_harness_routes(self):
        """Register agent harness observability and manual tick routes"""
        if not AGENT_HARNESS_ROUTES_AVAILABLE:
            logger.warning("Agent harness routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_agent_harness_router(
            require_auth_dependency=require_auth,
            config=self.config if hasattr(self, "config") else {},
            get_db_manager=lambda: self._db_manager,
            is_admin_user=self._is_admin_user,
            get_coordinator=lambda: self._agent_work_coordinator,
        )
        self._agent_harness_router = router
        self.app.include_router(router)
        start_hook = getattr(router, "agent_harness_start", None)
        stop_hook = getattr(router, "agent_harness_stop", None)
        if start_hook:
            if stop_hook:
                self._register_lifecycle_pair(start_hook, stop_hook)
            else:
                self._startup_background_tasks.append(start_hook)
        elif stop_hook:
            self._shutdown_background_tasks.append(stop_hook)
        logger.info("Agent harness routes registered")

    def _register_apps_routes(self):
        """Register persistent App APIs."""
        if not APPS_ROUTES_AVAILABLE:
            logger.warning("Apps routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        # App Action runs are persisted and dispatched by the execution
        # service.  Keep the owner/device-bound PC transport behind this
        # server seam so routes never construct AppActionRun rows directly.
        execution_service = getattr(self, "_app_action_execution_service", None)
        if execution_service is None:
            try:
                from ..services.app_action_runtime_service import (
                    build_app_action_execution_service,
                )
                transport_config = (
                    self.config
                    if isinstance(self.config, dict)
                    else getattr(self.config, "config", {})
                )
                execution_service = build_app_action_execution_service(
                    self._db_manager,
                    config=transport_config,
                    workspace_root=self._resolve_workspace_root(),
                )
            except Exception:
                logger.exception("App Action execution service initialization failed")
                execution_service = None
        self._app_action_execution_service = execution_service

        if execution_service is not None:
            # RunService commits its dispatch fence before invoking transport.
            # A preflight callback can therefore fail closed on a stale
            # Gateway/PC registration without ever sending a start request.
            if getattr(execution_service, "preflight", None) is None:
                transport = getattr(execution_service, "transport", None)

                async def _app_action_preflight(run):
                    checker = getattr(execution_service, "preflight_run", None)
                    if callable(checker):
                        return await checker(run)
                    availability = getattr(transport, "availability", None)
                    if not callable(availability):
                        return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
                    try:
                        result = await availability(
                            owner_user_id=str(getattr(run, "owner_user_id", None) or getattr(run, "started_by", "")),
                            device_id=str(getattr(run, "device_id", "")),
                            machine_id=str(getattr(run, "machine_id", "")),
                            execution_target_id=str(getattr(run, "execution_target_id", "")),
                            execution_target_kind=str(getattr(run, "execution_target_kind", "")),
                            execution_mode=str(getattr(run, "execution_mode", "finite")),
                            saved_action_id=getattr(run, "saved_action_id", None),
                            sha256=getattr(run, "target_sha256", None),
                            contract_hash=getattr(run, "target_contract_hash", None),
                            mode=str(getattr(run, "execution_mode", "finite")),
                            input_sha256=getattr(run, "input_hash", None),
                        )
                    except Exception:
                        return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
                    if isinstance(result, dict):
                        return {
                            **result,
                            "allowed": bool(result.get("online", result.get("available", False))),
                            "error_code": (
                                None
                                if result.get("online", result.get("available", False))
                                else "app_action_target_unavailable"
                            ),
                        }
                    return {"allowed": bool(result), "error_code": None if result else "app_action_target_unavailable"}

                execution_service.preflight = _app_action_preflight

            recover = getattr(execution_service, "recover_pending_runs", None)
            if callable(recover):
                async def _recover_app_action_runs() -> None:
                    try:
                        await recover()
                    except Exception:
                        # Recovery is status-only and must not prevent the API
                        # from starting.  The run remains observable as
                        # unknown/blocked for a later explicit reconcile.
                        logger.exception("App Action run recovery failed")

                async def _stop_app_action_recovery() -> None:
                    return None

                self._register_lifecycle_pair(
                    _recover_app_action_runs,
                    _stop_app_action_recovery,
                )
        router = create_apps_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            get_llm_client=lambda: getattr(self, "_llm_client", None),
            workspace_root=self._resolve_workspace_root(),
            get_app_config=lambda: getattr(self, "config", {}) or {},
            execution_service=execution_service,
        )
        self.app.include_router(router)
        logger.info("Persistent Apps routes registered")

    def _register_ecc_routes(self):
        """Register ECC feature routes (agents, automations, usage, workflows, etc.)"""
        if not ECC_ROUTES_AVAILABLE:
            logger.warning("ECC routes not available")
            return

        router = create_ecc_router(self)
        self.app.include_router(router)
        logger.info("ECC feature routes registered (49 endpoints)")

    def _register_generated_media_routes(self):
        """Register durable generated media delivery routes."""
        from .routes.generated_media_routes import build_generated_media_router

        router = build_generated_media_router(
            self._enforce_cookie_auth,
            self._get_user_info_from_request,
        )
        self.app.include_router(router)
        logger.info("Generated media routes registered")

    def _register_comfyui_routes(self):
        """Register ComfyUI management API routes"""
        if not COMFYUI_ROUTES_AVAILABLE:
            logger.warning("ComfyUI routes not available")
            return

        router = create_comfyui_router(self)
        self.app.include_router(router)
        logger.info("ComfyUI routes registered")
    def get_app(self):
        """Get FastAPI app instance"""
        self._register_generated_images_route()
        self._register_static_mounts()
        return self.app
    def _register_generated_images_route(self):
        """旧 temp 配信経路は廃止し、正規 media API へ誘導する。"""

        @self.app.get("/api/generated-images/{filename}")
        async def serve_generated_image_legacy(filename: str, request: Request):
            self._enforce_cookie_auth(request)
            raise HTTPException(
                status_code=410,
                detail="この画像配信経路は廃止されました。/api/generated-media/{id} を使用してください。",
            )

    def _register_static_mounts(self):
        """Register static mounts (minimal - frontend is served by Next.js)."""
        if not self._static_mounts_registered:
            self._static_mounts_registered = True


def create_web_interface(config, character_name: str):
    """Factory function for WebChatServer"""
    runtime_feature_manager.configure(config)
    return WebChatServer(config, character_name)
