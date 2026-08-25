#!/usr/bin/env python3
"""
FastAPI + WebSocket server for AoiTalk Web Interface

WebChatServer 本体（合成クラス）。実際の振る舞いは server_parts/ 配下の Mixin へ委譲し、
このファイルには __init__・ライフサイクル・ルート登録オーケストレーションのみを残す。
モジュールレベルの import / 可用性フラグ / logger は server_shared に集約している。
"""

import ipaddress
import re
from urllib.parse import urlsplit

from .server_shared import *  # noqa: F401,F403
from ..features import Features
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
        self._task_notification_worker = None
        try:
            from ..heartbeat.runner import get_heartbeat_runner

            heartbeat_config = (
                config.get("heartbeat", {}) if hasattr(config, "get") else {}
            )
            if heartbeat_config.get("enabled", True):
                self._heartbeat_runner = get_heartbeat_runner()
        except Exception as e:
            logger.warning(f"Heartbeat runner initialization skipped: {e}")
        self._startup_background_tasks: list[Any] = []
        self._shutdown_background_tasks: list[Any] = []
        self._content_retention_worker = None
        try:
            from ..services.content_retention_worker import ContentRetentionWorker

            self._content_retention_worker = ContentRetentionWorker()
            self._startup_background_tasks.append(
                self._content_retention_worker.start
            )
            self._shutdown_background_tasks.append(
                self._content_retention_worker.stop
            )
        except Exception as exc:
            # Retention housekeeping is intentionally optional during a
            # rolling deploy where the Python task purge helper may not yet be
            # importable.  File operations themselves remain fail-closed.
            logger.warning("コンテンツ保持期間ワーカーを登録できませんでした: %s", exc)
        self._mage_vl_preload_factory: Any = None
        self._mage_vl_preload_task: asyncio.Task[Any] | None = None
        self._register_mage_vl_lifecycle()
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
            # Startup
            with _startup_timer.phase("startup.web.lifespan.on_startup"):
                await self._on_startup()
            with _startup_timer.phase("startup.web.lifespan.dispatch_recovery"):
                await self._start_conversation_dispatch_recovery()
            # Start heartbeat runner
            if self._heartbeat_runner:
                try:
                    with _startup_timer.phase("startup.web.lifespan.heartbeat_start"):
                        await self._heartbeat_runner.start()
                    logger.info("Heartbeat runner started")
                except Exception as e:
                    logger.error(f"Heartbeat runner start failed: {e}")
            if self._task_notification_worker:
                try:
                    with _startup_timer.phase("startup.web.lifespan.notification_worker_start"):
                        await self._task_notification_worker.start()
                except Exception as e:
                    logger.error(f"Task notification worker start failed: {e}")
            yield
            # Shutdown
            await self._stop_conversation_dispatch_recovery()
            if self._task_notification_worker:
                try:
                    await self._task_notification_worker.stop()
                except Exception as e:
                    logger.error(f"Task notification worker stop failed: {e}")
            if self._heartbeat_runner:
                try:
                    await self._heartbeat_runner.stop()
                except Exception as e:
                    logger.error(f"Heartbeat runner stop failed: {e}")
            pending_shutdown_tasks = list(self._shutdown_background_tasks)
            self._shutdown_background_tasks.clear()
            for task_factory in pending_shutdown_tasks:
                try:
                    await task_factory()
                except Exception as exc:
                    logger.exception("Shutdown task failed: %s", exc)

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
        self.legacy_cookie_name = "aoitalk_session"
        self.next_cookie_name = "aoitalk_session"

        # Connection manager
        self.manager = ConnectionManager()
        self.manager.set_authorization_checker(self._websocket_connection_allowed)
        self.manager.set_admin_role_checker(self._websocket_is_admin_user)

        from .trpg_play_connection_manager import TrpgPlayConnectionManager

        self.trpg_play_manager = TrpgPlayConnectionManager()

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
            )

        # Callbacks
        self.on_user_input = None
        self.on_clear_chat = None  # Callback for clear chat events
        self.on_llm_client_change = None
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

        # Setup routes
        self._setup_routes()

        # Register project routes if available
        if PROJECT_ROUTES_AVAILABLE and create_project_router:
            self._register_project_routes()

        # Register Project-scoped Docs candidate review routes immediately
        # after the base Project routes.  These endpoints intentionally do
        # not use the generic memory candidate decision API.
        if (
            PROJECT_DOCS_CANDIDATE_ROUTES_AVAILABLE
            and create_project_docs_candidate_router
        ):
            self._register_project_docs_candidate_routes()

        # Register ProjectContextPack projection status/rebuild routes after
        # the base Project routes.  They expose metadata only and are
        # intentionally independent from the canonical Docs candidate queue.
        if (
            PROJECT_CONTEXT_PACK_ROUTES_AVAILABLE
            and create_project_context_pack_router
        ):
            self._register_project_context_pack_routes()

        # Register Knowledge Workspace routes if available
        if KNOWLEDGE_ROUTES_AVAILABLE and create_knowledge_router:
            self._register_knowledge_routes()

        # Register Deep Research routes if available
        if DEEP_RESEARCH_ROUTES_AVAILABLE and create_deep_research_router:
            self._register_deep_research_routes()

        # Register conversation routes if available
        if CONVERSATION_ROUTES_AVAILABLE and create_conversation_router:
            self._register_conversation_routes()

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

        # Register authenticated per-user X Cookie management routes.
        if X_COOKIE_ROUTES_AVAILABLE and create_x_cookie_router:
            self._register_x_cookie_routes()

        # Register heartbeat routes if available
        if HEARTBEAT_ROUTES_AVAILABLE and create_heartbeat_router:
            self._register_heartbeat_routes()

        # Register agent harness status routes if available
        if AGENT_HARNESS_ROUTES_AVAILABLE and create_agent_harness_router:
            self._register_agent_harness_routes()

        if APPS_ROUTES_AVAILABLE and create_apps_router:
            self._register_apps_routes()

        # Register hydrus browser routes if available
        if HYDRUS_ROUTES_AVAILABLE and create_hydrus_router:
            self._register_hydrus_routes()

        # Register ECC feature routes if available
        if ECC_ROUTES_AVAILABLE and create_ecc_router:
            self._register_ecc_routes()

        # Register Scenario Studio canonical routes.
        if STORY_ROUTES_AVAILABLE and create_story_router:
            self._register_story_routes()
        if STORY_LEGACY_COMPAT_AVAILABLE and create_story_legacy_compat_router:
            self._register_story_legacy_compat_routes()

        # Register read-only TRPG asset reference routes; play execution was retired.
        if TRPG_REFERENCE_ROUTES_AVAILABLE and create_trpg_reference_router:
            self._register_trpg_reference_routes()
        if TRPG_PLAY_ROUTES_AVAILABLE and create_trpg_play_router:
            self._register_trpg_play_routes()

        # Register comfyui routes if available
        if COMFYUI_ROUTES_AVAILABLE and create_comfyui_router:
            self._register_comfyui_routes()
        self._register_generated_media_routes()

        # Register the frontend catch-all last so it does not shadow API routers.
        self._register_frontend_catchall()

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
            await shutdown_mage_vl_services()

        self._mage_vl_preload_factory = _preload_mage_vl
        self._startup_background_tasks.append(_preload_mage_vl)
        self._shutdown_background_tasks.append(_shutdown_mage_vl)

        async def _flush_docs_reindex_on_startup() -> None:
            try:
                from ..rag.docs_index import flush_pending_docs_reindex

                await flush_pending_docs_reindex()
            except Exception as exc:
                logger.debug("Docs reindex startup flush skipped: %s", exc)

        self._startup_background_tasks.append(_flush_docs_reindex_on_startup)

    async def _on_startup(self):
        """Startup event handler - ensures admin user exists"""
        pending_background_tasks = list(self._startup_background_tasks)
        self._startup_background_tasks.clear()
        with _startup_timer.phase("startup.web.lifespan.background_schedule"):
            for task_factory in pending_background_tasks:
                try:
                    task = asyncio.create_task(task_factory())
                    if task_factory is self._mage_vl_preload_factory:
                        self._mage_vl_preload_task = task
                except Exception as exc:
                    logger.error(f"Failed to schedule startup background task: {exc}")

        # Story Studio jobs must not remain ``running`` after a process restart.
        # Mark them interrupted before normal request handling resumes so the UI
        # can offer the documented resume action without touching any GET route.
        if not Features.is_enterprise():
            try:
                from ..services.story_studio import StoryJobRunner

                if self._db_manager is not None:
                    session = await self._db_manager.get_session()
                    try:
                        with _startup_timer.phase(
                            "startup.web.lifespan.story_recovery"
                        ):
                            interrupted = await StoryJobRunner(session).mark_interrupted()
                            await session.commit()
                        if interrupted:
                            logger.info(
                                "Story Studio の中断ジョブを %s 件復旧しました",
                                interrupted,
                            )
                    finally:
                        await session.close()
            except Exception as exc:
                logger.warning("Story Studio の中断ジョブ復旧をスキップしました: %s", exc)

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

            asyncio.create_task(_refresh_openrouter_pricing())
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

        if self._task_notification_worker:
            try:
                with _startup_timer.phase("startup.web.lifespan.notification_worker_sync"):
                    await self._task_notification_worker.run_once()
            except Exception as exc:
                logger.error(f"Task startup sync failed: {exc}")

    def _setup_routes(self):
        """Setup API routes (ドメイン別の registrar モジュールへ委譲)"""
        register_system_routes(self.app, self)
        register_config_routes(self.app, self)
        register_chatgpt_web_routes(self.app, self)
        register_yomi_linter_routes(self.app, self)
        register_llm_routes(self.app, self)
        register_free_team_routes(self.app, self)
        if Features.crawler_status():
            register_crawler_routes(self.app, self)
        if not Features.is_enterprise():
            register_mobile_command_routes(self.app, self)
        register_conversation_dispatch_routes(self.app, self)
        register_agent_run_routes(self.app, self)
        register_live_voice_routes(self.app, self)
        register_voice_session_routes(self.app, self)
        register_file_explorer_routes(self.app, self)
        register_ogp_routes(self.app, self)
        register_document_storage_routes(self.app, self)
        register_auth_routes(self.app, self)
        register_api_token_routes(self.app, self)
        register_capabilities_routes(self.app, self)
        if not Features.is_enterprise():
            register_remote_server_routes(self.app, self)
            register_remote_proxy_routes(self.app, self)
        register_user_admin_routes(self.app, self)
        register_feedback_routes(self.app, self)
        register_websocket_routes(self.app, self)
        if TRPG_PLAY_WEBSOCKET_ROUTES_AVAILABLE and register_trpg_play_websocket_routes:
            register_trpg_play_websocket_routes(self.app, self)

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

    def _register_project_context_pack_routes(self):
        """Register ProjectContextPack projection status/rebuild routes."""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_project_context_pack_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("ProjectContextPack routes registered")

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
        )
        self.app.include_router(router)
        logger.info("Conversation routes registered")

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
                startup_tasks = getattr(self, "_startup_background_tasks", None)
                if startup_tasks is None:
                    startup_tasks = self._startup_background_tasks = []
                shutdown_tasks = getattr(self, "_shutdown_background_tasks", None)
                if shutdown_tasks is None:
                    shutdown_tasks = self._shutdown_background_tasks = []
                startup_tasks.append(worker.start)
                shutdown_tasks.append(worker.stop)

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
        )
        self.app.include_router(router)
        start_hook = getattr(router, "agent_harness_start", None)
        stop_hook = getattr(router, "agent_harness_stop", None)
        if start_hook:
            self._startup_background_tasks.append(start_hook)
        if stop_hook:
            self._shutdown_background_tasks.append(stop_hook)
        logger.info("Agent harness routes registered")

    def _register_apps_routes(self):
        """Register persistent App APIs."""
        if not APPS_ROUTES_AVAILABLE:
            logger.warning("Apps routes not available")
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_apps_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            get_llm_client=lambda: getattr(self, "_llm_client", None),
            workspace_root=self._resolve_workspace_root(),
            get_app_config=lambda: getattr(self, "config", {}) or {},
        )
        self.app.include_router(router)
        logger.info("Persistent Apps routes registered")

    def _register_hydrus_routes(self):
        """Register Hydrus Browser API routes"""
        if not HYDRUS_ROUTES_AVAILABLE:
            logger.warning("Hydrus browser routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_hydrus_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
        )
        self.app.include_router(router)
        compat_router = create_hydrus_compat_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
        )
        self.app.include_router(compat_router)
        logger.info("Hydrus browser routes registered")

    def _register_ecc_routes(self):
        """Register ECC feature routes (agents, automations, usage, workflows, etc.)"""
        if not ECC_ROUTES_AVAILABLE:
            logger.warning("ECC routes not available")
            return

        router = create_ecc_router(self)
        self.app.include_router(router)
        logger.info("ECC feature routes registered (49 endpoints)")

    def _register_story_routes(self):
        """Register Scenario Studio canonical story routes."""
        if not STORY_ROUTES_AVAILABLE:
            logger.warning("Scenario Studio routes not available")
            return

        router = create_story_router(self)
        self.app.include_router(router)
        logger.info("Scenario Studio routes registered")

        if STORY_ASSIST_ROUTES_AVAILABLE and create_story_assist_router:
            assist_router = create_story_assist_router(self)
            self.app.include_router(assist_router)
            logger.info("Scenario Studio assist routes registered")

    def _register_story_legacy_compat_routes(self):
        """Register read-only mobile/legacy story projections.

        mobile が実際に叩く GET /api/scenarios/{id} と /api/scenarios/{id}/canon
        を提供する（scenario_routes.py 削除後の 404 を塞ぐ）。書き込み系は無い。
        """
        if not STORY_LEGACY_COMPAT_AVAILABLE:
            return
        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_story_legacy_compat_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info(
            "Story legacy compatibility routes registered "
            "(GET /api/scenarios/{id}, /api/scenarios/{id}/canon)"
        )

    def _register_trpg_reference_routes(self):
        """Register read-only TRPG rules and reference asset routes."""
        if not TRPG_REFERENCE_ROUTES_AVAILABLE or not create_trpg_reference_router:
            return
        self.app.include_router(create_trpg_reference_router())
        logger.info("TRPG reference asset routes registered")

    def _register_trpg_play_routes(self):
        """Register TRPG Play execution routes."""
        if not TRPG_PLAY_ROUTES_AVAILABLE or not create_trpg_play_router:
            return
        router = create_trpg_play_router(self)
        self.app.include_router(router)
        logger.info("TRPG Play routes registered")

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
