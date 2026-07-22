#!/usr/bin/env python3
"""
FastAPI + WebSocket server for AoiTalk Web Interface

WebChatServer 本体（合成クラス）。実際の振る舞いは server_parts/ 配下の Mixin へ委譲し、
このファイルには __init__・ライフサイクル・ルート登録オーケストレーションのみを残す。
モジュールレベルの import / 可用性フラグ / logger は server_shared に集約している。
"""

from .server_shared import *  # noqa: F401,F403
from .server_parts import (
    AuthMixin,
    ChatMessageMixin,
    ConversationMixin,
    MessagingMixin,
    MobileCommandsMixin,
)


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
        self._conversation_dispatch_tasks: set[Any] = set()
        self._conversation_generation_tasks: Dict[str, Set[Any]] = {}
        self._conversation_generation_status: Dict[str, Dict[str, Any]] = {}
        self._conversation_steering_queues: Dict[str, List[str]] = {}

        # Create lifespan context manager
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """Lifespan event handler for startup/shutdown"""
            # Startup
            await self._on_startup()
            # Start heartbeat runner
            if self._heartbeat_runner:
                try:
                    await self._heartbeat_runner.start()
                    logger.info("Heartbeat runner started")
                except Exception as e:
                    logger.error(f"Heartbeat runner start failed: {e}")
            if self._task_notification_worker:
                try:
                    await self._task_notification_worker.start()
                except Exception as e:
                    logger.error(f"Task notification worker start failed: {e}")
            yield
            # Shutdown
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

        # LLM client reference (will be set by terminal/voice mode)
        self._llm_client = None
        self._current_llm_mode = "fast"  # 'fast' or 'thinking'
        self._ollama_model_manager = OllamaModelManager(config)

        # Setup routes
        self._setup_routes()

        # Register project routes if available
        if PROJECT_ROUTES_AVAILABLE and create_project_router:
            self._register_project_routes()

        # Register Knowledge Workspace routes if available
        if KNOWLEDGE_ROUTES_AVAILABLE and create_knowledge_router:
            self._register_knowledge_routes()

        # Register Deep Research routes if available
        if DEEP_RESEARCH_ROUTES_AVAILABLE and create_deep_research_router:
            self._register_deep_research_routes()

        # Register git routes if available
        if GIT_ROUTES_AVAILABLE and create_git_router:
            self._register_git_routes()

        # Register conversation routes if available
        if CONVERSATION_ROUTES_AVAILABLE and create_conversation_router:
            self._register_conversation_routes()

        # Register group chat routes if available
        if GROUP_CHAT_ROUTES_AVAILABLE and create_group_chat_router:
            self._register_group_chat_routes()

        # Register skill routes if available
        if SKILL_ROUTES_AVAILABLE and create_skill_router:
            self._register_skill_routes()

        # Register task event routes if available
        if TASK_ROUTES_AVAILABLE and create_task_router:
            self._register_task_routes()

        # Register mobile sync routes after task routes; it reuses task service semantics.
        if SYNC_ROUTES_AVAILABLE and create_sync_router:
            self._register_sync_routes()

        # Register Docs REST routes (shares apply_docs_operation with sync push).
        if DOCS_ROUTES_AVAILABLE and create_docs_router:
            self._register_docs_routes()

        # Register heartbeat routes if available
        if HEARTBEAT_ROUTES_AVAILABLE and create_heartbeat_router:
            self._register_heartbeat_routes()

        # Register agent harness status routes if available
        if AGENT_HARNESS_ROUTES_AVAILABLE and create_agent_harness_router:
            self._register_agent_harness_routes()

        # Register app factory artifact routes if available
        if APP_FACTORY_ROUTES_AVAILABLE and create_app_factory_router:
            self._register_app_factory_routes()

        # Register hydrus browser routes if available
        if HYDRUS_ROUTES_AVAILABLE and create_hydrus_router:
            self._register_hydrus_routes()

        # Register ECC feature routes if available
        if ECC_ROUTES_AVAILABLE and create_ecc_router:
            self._register_ecc_routes()

        # Register scenario routes if available
        if SCENARIO_ROUTES_AVAILABLE and create_scenario_router:
            self._register_scenario_routes()

        # Register TRPG play (multiplayer) routes if available
        if TRPG_PLAY_ROUTES_AVAILABLE and create_trpg_play_router:
            self._register_trpg_play_routes()

        # Register comfyui routes if available
        if COMFYUI_ROUTES_AVAILABLE and create_comfyui_router:
            self._register_comfyui_routes()

        # Register the frontend catch-all last so it does not shadow API routers.
        self._register_frontend_catchall()

    async def _on_startup(self):
        """Startup event handler - ensures admin user exists"""
        pending_background_tasks = list(self._startup_background_tasks)
        self._startup_background_tasks.clear()
        for task_factory in pending_background_tasks:
            try:
                asyncio.create_task(task_factory())
            except Exception as exc:
                logger.error(f"Failed to schedule startup background task: {exc}")

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            logger.info("Admin initialization skipped: UserRepository not available")
            return

        try:
            session = await self._db_manager.get_session()
            try:
                admin_created = await UserRepository.ensure_admin_exists(session)
                if admin_created:
                    logger.warning(
                        "⚠️  初期管理者を作成しました: admin / admin123\n"
                        "⚠️  セキュリティのため、ログイン後すぐにパスワードを変更してください！"
                    )
                else:
                    logger.info("Admin user already exists")

            except Exception as e:
                logger.error(f"Failed to ensure admin exists: {e}")
            finally:
                await session.close()
        except Exception as e:
            logger.error(
                f"Failed to get database session for admin initialization: {e}"
            )

        if self._task_notification_worker:
            try:
                await self._task_notification_worker.run_once()
            except Exception as exc:
                logger.error(f"Task startup sync failed: {exc}")

    def _setup_routes(self):
        """Setup API routes (ドメイン別の registrar モジュールへ委譲)"""
        register_system_routes(self.app, self)
        register_config_routes(self.app, self)
        register_yomi_linter_routes(self.app, self)
        register_llm_routes(self.app, self)
        register_free_team_routes(self.app, self)
        register_crawler_routes(self.app, self)
        register_mobile_command_routes(self.app, self)
        register_conversation_dispatch_routes(self.app, self)
        register_agent_run_routes(self.app, self)
        register_file_explorer_routes(self.app, self)
        register_ogp_routes(self.app, self)
        register_document_storage_routes(self.app, self)
        register_auth_routes(self.app, self)
        register_api_token_routes(self.app, self)
        register_capabilities_routes(self.app, self)
        register_remote_server_routes(self.app, self)
        register_remote_proxy_routes(self.app, self)
        register_user_admin_routes(self.app, self)
        register_feedback_routes(self.app, self)
        register_websocket_routes(self.app, self)

    def _build_cors_origins(self) -> List[str]:
        """CORS の許可オリジン一覧を組み立てる。

        - 環境変数 AOITALK_CORS_ORIGINS（カンマ区切り）があれば最優先で使用する。
        - なければローカル開発用デフォルトに、config の公開URL設定
          （web_interface.public_url）があれば追加する。
        """
        env_origins = os.environ.get("AOITALK_CORS_ORIGINS", "")
        if env_origins.strip():
            return [
                origin.strip().rstrip("/")
                for origin in env_origins.split(",")
                if origin.strip()
            ]

        origins = ["http://127.0.0.1:3002", "http://localhost:3002"]
        try:
            public_url = None
            if hasattr(self.config, "get"):
                public_url = self.config.get("web_interface.public_url", None)
            elif isinstance(self.config, dict):
                public_url = (
                    self.config.get("web_interface", {}) or {}
                ).get("public_url")
            if isinstance(public_url, str) and public_url.strip():
                origin = public_url.strip().rstrip("/")
                if origin not in origins:
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
    def _register_project_routes(self):
        """Register project API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_project_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Project routes registered")

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

    def _register_git_routes(self):
        """Register Git API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_git_router(
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Git routes registered")

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

    def _register_task_routes(self):
        """Register task management API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_task_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
            broadcaster=self.manager.broadcast,
        )
        self.app.include_router(router)
        logger.info("Task management routes registered")

    def _register_sync_routes(self):
        """Register mobile sync API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_sync_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Mobile sync routes registered")

    def _register_docs_routes(self):
        """Register Docs REST API routes"""

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_docs_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth,
        )
        self.app.include_router(router)
        logger.info("Docs routes registered")

    def _register_heartbeat_routes(self):
        """Register Heartbeat API routes"""
        if not HEARTBEAT_ROUTES_AVAILABLE:
            logger.warning("Heartbeat routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_heartbeat_router(require_auth=require_auth)
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
        )
        self.app.include_router(router)
        start_hook = getattr(router, "agent_harness_start", None)
        stop_hook = getattr(router, "agent_harness_stop", None)
        if start_hook:
            self._startup_background_tasks.append(start_hook)
        if stop_hook:
            self._shutdown_background_tasks.append(stop_hook)
        logger.info("Agent harness routes registered")

    def _register_app_factory_routes(self):
        """Register app factory artifact routes."""
        if not APP_FACTORY_ROUTES_AVAILABLE:
            logger.warning("App factory routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)
        router = create_app_factory_router(
            require_auth_dependency=require_auth,
            config=self.config if hasattr(self, "config") else {},
        )
        self.app.include_router(router)
        logger.info("App factory routes registered")

    def _register_hydrus_routes(self):
        """Register Hydrus Browser API routes"""
        if not HYDRUS_ROUTES_AVAILABLE:
            logger.warning("Hydrus browser routes not available")
            return

        require_auth = cookie_auth_dependency(self._enforce_cookie_auth)

        router = create_hydrus_router(require_auth=require_auth)
        self.app.include_router(router)
        compat_router = create_hydrus_compat_router()
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

    def _register_scenario_routes(self):
        """Register scenario (TRPG) API routes"""
        if not SCENARIO_ROUTES_AVAILABLE:
            logger.warning("Scenario routes not available")
            return

        router = create_scenario_router(self)
        self.app.include_router(router)
        logger.info("Scenario routes registered")

    def _register_trpg_play_routes(self):
        """Register TRPG multiplayer play API routes + WebSocket room endpoint"""
        if not TRPG_PLAY_ROUTES_AVAILABLE:
            logger.warning("TRPG play routes not available")
            return

        router = create_trpg_play_router(self)
        self.app.include_router(router)
        try:
            from .trpg_ws import register_trpg_ws

            register_trpg_ws(self.app, self)
            logger.info("TRPG play routes + WS registered")
        except Exception as e:  # noqa: BLE001
            logger.warning("TRPG WS registration failed: %s", e)

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
        """Register route for serving generated images from temp directory."""
        import re

        @self.app.get("/api/generated-images/{filename}")
        async def serve_generated_image(filename: str, request: Request):
            self._enforce_cookie_auth(request)

            # ファイル名バリデーション（パストラバーサル防止）
            if not re.match(r"^[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+$", filename):
                raise HTTPException(status_code=400, detail="無効なファイル名です")

            image_dir = Path("temp/generated_images")
            file_path = (image_dir / filename).resolve()

            # ディレクトリトラバーサル防止
            if not str(file_path).startswith(str(image_dir.resolve())):
                raise HTTPException(status_code=400, detail="無効なファイルパスです")

            if not file_path.exists():
                raise HTTPException(status_code=404, detail="画像が見つかりません")

            return FileResponse(
                path=str(file_path),
                media_type=_guess_image_media_type(filename),
            )

        def _guess_image_media_type(filename: str) -> str:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            return {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(ext, "application/octet-stream")

    def _register_static_mounts(self):
        """Register static mounts (minimal - frontend is served by Next.js)."""
        if not self._static_mounts_registered:
            self._static_mounts_registered = True


def create_web_interface(config, character_name: str):
    """Factory function for WebChatServer"""
    runtime_feature_manager.configure(config)
    return WebChatServer(config, character_name)
