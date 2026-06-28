#!/usr/bin/env python3
"""
FastAPI + WebSocket server for AoiTalk Web Interface
"""

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import asynccontextmanager
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from pathlib import Path
from uuid import UUID

import jwt
from fastapi import (
    FastAPI,
    WebSocket,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .router_helpers import cookie_auth_dependency
from .connection_manager import ConnectionManager
from .routes.api_token_routes import register_api_token_routes
from .routes.auth_routes import register_auth_routes
from .routes.agent_run_routes import register_agent_run_routes
from .routes.capabilities_routes import register_capabilities_routes
from .routes.config_routes import register_config_routes
from .routes.conversation_dispatch_routes import register_conversation_dispatch_routes
from .routes.crawler_routes import register_crawler_routes
from .routes.remote_server_routes import register_remote_server_routes
from .routes.remote_proxy_routes import register_remote_proxy_routes
from .routes.document_storage_routes import register_document_storage_routes
from .routes.feedback_routes import register_feedback_routes
from .routes.file_explorer_routes import register_file_explorer_routes
from .routes.llm_routes import register_llm_routes
from .routes.mobile_command_routes import register_mobile_command_routes
from .routes.ogp_routes import register_ogp_routes
from .routes.payloads import (
    effective_include_project_context,
    sanitize_response_model_selection,
)
from .routes.system_routes import register_system_routes
from .routes.user_admin_routes import register_user_admin_routes
from .routes.websocket_routes import register_websocket_routes
from ..services.conversation_title_llm import generate_title_with_llm_client
from ..services.llm_model_catalog import build_llm_mode_state
from ..services.ollama_model_service import OllamaModelManager
from ..runtime_features import runtime_feature_manager
from ..assistant.chat_attachment_utils import sanitize_chat_attachments

# Import CharacterSwitchManager
try:
    from ..tools.keyword.character_manager import CharacterSwitchManager
except ImportError:
    # Fallback for direct execution
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).parent.parent))
    from tools.keyword.character_manager import CharacterSwitchManager

# Import database and login log repository
try:
    from ..memory.database import get_database_manager
    from ..memory.login_log_repository import LoginLogRepository
    from ..memory.user_repository import UserRepository

    USER_REPOSITORY_AVAILABLE = True
except ImportError:
    # Fallback for environments without database
    get_database_manager = None
    LoginLogRepository = None
    UserRepository = None
    USER_REPOSITORY_AVAILABLE = False

# Import authentication service
try:
    from .auth_service import get_auth_service

    AUTH_SERVICE_AVAILABLE = True
except ImportError:
    AUTH_SERVICE_AVAILABLE = False
    get_auth_service = None

# Import long-lived API token support (server-to-server access)
try:
    from ..memory.api_token_repository import (
        TOKEN_PREFIX as LONG_LIVED_TOKEN_PREFIX,
        ApiTokenRepository,
    )

    API_TOKEN_REPOSITORY_AVAILABLE = True
except ImportError:
    API_TOKEN_REPOSITORY_AVAILABLE = False
    ApiTokenRepository = None
    LONG_LIVED_TOKEN_PREFIX = "aoitpat_"

# Import BGM callback registry
try:
    from ..utils.audio_globals import set_bgm_callback
except ImportError:
    set_bgm_callback = None

# Import file explorer service (@メンションのファイル参照展開に使用)
try:
    from ..tools.file_explorer import get_full_content as explorer_get_full_content

    FILE_EXPLORER_AVAILABLE = True
except ImportError:
    FILE_EXPLORER_AVAILABLE = False
    explorer_get_full_content = None

# Import external LLM permission manager
try:
    from ..tools.external_llm_permission import (
        ExternalLLMPermissionManager,
        set_permission_manager,
    )

    EXTERNAL_LLM_PERMISSION_AVAILABLE = True
except ImportError:
    EXTERNAL_LLM_PERMISSION_AVAILABLE = False
    ExternalLLMPermissionManager = None
    set_permission_manager = None

from ..llm.generation_policy import resolve_generation_profile
from ..llm.tool_policy import (
    build_command_capability_context,
    command_capabilities_for_current_turn_text,
    sanitize_command_capabilities,
)

# Import os_operations user context functions
try:
    from ..tools.os_operations.tools import set_current_user_context

    OS_OPS_CONTEXT_AVAILABLE = True
except ImportError:
    OS_OPS_CONTEXT_AVAILABLE = False
    set_current_user_context = None

# Import Knowledge project context
try:
    from ..tools.knowledge import set_current_project_context as set_knowledge_project_context

    KNOWLEDGE_PROJECT_CONTEXT_AVAILABLE = True
except ImportError:
    set_knowledge_project_context = None
    KNOWLEDGE_PROJECT_CONTEXT_AVAILABLE = False

# Import project routes
try:
    from .project_routes import create_project_router

    PROJECT_ROUTES_AVAILABLE = True
except ImportError:
    PROJECT_ROUTES_AVAILABLE = False
    create_project_router = None

# Import Knowledge routes
try:
    from .knowledge_routes import create_knowledge_router

    KNOWLEDGE_ROUTES_AVAILABLE = True
except ImportError:
    KNOWLEDGE_ROUTES_AVAILABLE = False
    create_knowledge_router = None

# Import Deep Research routes
try:
    from .deep_research_routes import create_deep_research_router

    DEEP_RESEARCH_ROUTES_AVAILABLE = True
except ImportError:
    DEEP_RESEARCH_ROUTES_AVAILABLE = False
    create_deep_research_router = None

# Import git routes
try:
    from .git_routes import create_git_router

    GIT_ROUTES_AVAILABLE = True
except ImportError:
    GIT_ROUTES_AVAILABLE = False
    create_git_router = None

# Import conversation routes
try:
    from .conversation_routes import create_conversation_router

    CONVERSATION_ROUTES_AVAILABLE = True
except ImportError:
    CONVERSATION_ROUTES_AVAILABLE = False
    create_conversation_router = None

# Import group chat routes
try:
    from .group_chat_routes import create_group_chat_router

    GROUP_CHAT_ROUTES_AVAILABLE = True
except ImportError:
    GROUP_CHAT_ROUTES_AVAILABLE = False
    create_group_chat_router = None

# Import skill routes
try:
    from .skill_routes import create_skill_router

    SKILL_ROUTES_AVAILABLE = True
except ImportError:
    SKILL_ROUTES_AVAILABLE = False
    create_skill_router = None

# Import task event routes
try:
    from .task_event_routes import create_task_router

    TASK_ROUTES_AVAILABLE = True
except ImportError:
    TASK_ROUTES_AVAILABLE = False
    create_task_router = None

# Import mobile sync routes
try:
    from .sync_routes import create_sync_router

    SYNC_ROUTES_AVAILABLE = True
except ImportError:
    SYNC_ROUTES_AVAILABLE = False
    create_sync_router = None

# Import task reminder worker
try:
    from ..services.task_notification_worker import TaskNotificationWorker

    TASK_NOTIFICATION_WORKER_AVAILABLE = True
except ImportError:
    TASK_NOTIFICATION_WORKER_AVAILABLE = False
    TaskNotificationWorker = None

# Import heartbeat routes
try:
    from .heartbeat_routes import create_heartbeat_router

    HEARTBEAT_ROUTES_AVAILABLE = True
except ImportError:
    HEARTBEAT_ROUTES_AVAILABLE = False
    create_heartbeat_router = None

# Import agent harness routes
try:
    from .agent_harness_routes import create_agent_harness_router

    AGENT_HARNESS_ROUTES_AVAILABLE = True
except ImportError:
    AGENT_HARNESS_ROUTES_AVAILABLE = False
    create_agent_harness_router = None

# Import app factory artifact routes
try:
    from .app_factory_routes import create_app_factory_router

    APP_FACTORY_ROUTES_AVAILABLE = True
except ImportError:
    APP_FACTORY_ROUTES_AVAILABLE = False
    create_app_factory_router = None

# Import hydrus browser routes
try:
    from ..tools.hydrus_browser import create_hydrus_compat_router, create_hydrus_router

    HYDRUS_ROUTES_AVAILABLE = True
except ImportError:
    HYDRUS_ROUTES_AVAILABLE = False
    create_hydrus_compat_router = None
    create_hydrus_router = None

# Import ECC feature routes
try:
    from .ecc_routes import create_ecc_router

    ECC_ROUTES_AVAILABLE = True
except ImportError:
    ECC_ROUTES_AVAILABLE = False
    create_ecc_router = None

# Import scenario routes
try:
    from .scenario_routes import create_scenario_router

    SCENARIO_ROUTES_AVAILABLE = True
except ImportError:
    SCENARIO_ROUTES_AVAILABLE = False
    create_scenario_router = None

# Import TRPG play (multiplayer) routes
try:
    from .trpg_play_routes import create_trpg_play_router

    TRPG_PLAY_ROUTES_AVAILABLE = True
except ImportError:
    TRPG_PLAY_ROUTES_AVAILABLE = False
    create_trpg_play_router = None

# Import comfyui routes
try:
    from .comfyui_routes import create_comfyui_router

    COMFYUI_ROUTES_AVAILABLE = True
except ImportError:
    COMFYUI_ROUTES_AVAILABLE = False
    create_comfyui_router = None

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress noisy websockets library errors for expected disconnections
logging.getLogger("websockets.legacy.protocol").setLevel(logging.WARNING)


class WebChatServer:
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
        register_llm_routes(self.app, self)
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

    async def _get_user_info_from_websocket(
        self, websocket: WebSocket
    ) -> Optional[Dict[str, Any]]:
        """Resolve the authenticated user for a WebSocket connection."""
        if not self.auth_enabled:
            return {"id": "default_user", "username": "default_user", "role": "admin"}

        auth_header = websocket.headers.get("authorization")
        bearer_token = None
        if auth_header and auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:]
        else:
            bearer_token = websocket.query_params.get("token")
        if bearer_token:
            try:
                from .auth_service import get_auth_service

                payload = get_auth_service().verify_token(bearer_token)
                if payload and USER_REPOSITORY_AVAILABLE and self._db_manager:
                    db_session = await self._db_manager.get_session()
                    try:
                        user = await UserRepository.get_by_id(
                            db_session, UUID(payload.user_id)
                        )
                        return user.to_dict() if user else None
                    finally:
                        await db_session.close()
            except Exception:
                return None

        cookie_header = websocket.headers.get("cookie")
        next_session = self._get_cookie_from_header(cookie_header, self.next_cookie_name)
        next_payload = self._decode_next_session_cookie(next_session)
        next_user_id = next_payload.get("sub") if next_payload else None

        username = None
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        if not session_id:
            session_id = self._get_cookie_from_header(cookie_header, self.legacy_cookie_name)
        if session_id:
            serializer = self._get_serializer()
            if serializer:
                try:
                    session_data = serializer.loads(
                        session_id, max_age=self.session_ttl_seconds
                    )
                    username = session_data.get("u")
                except Exception:
                    username = None

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None

        db_session = await self._db_manager.get_session()
        try:
            user = None
            if next_user_id:
                user = await UserRepository.get_by_id(db_session, UUID(next_user_id))
            elif username:
                user = await UserRepository.get_by_username(db_session, username)
            return user.to_dict() if user else None
        finally:
            await db_session.close()

    async def _websocket_session_allowed(self, session_id: str, user_id: str) -> bool:
        try:
            from ..memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            return await repo.user_has_session_access(session_id, user_id)
        except Exception as e:
            logger.warning("WebSocket session access check failed: %s", e)
            return False

    async def _setup_user_context(self, websocket: WebSocket):
        """
        Set up user context for os_operations permission checks and LLM session.

        Extracts user info from session cookie and sets the context
        so that file operations respect user/project permissions,
        and LLM client uses the correct user_id for Dreaming memory.
        """
        # Default to admin (backward compat for unauthenticated or fallback)
        user_id = None
        username = None
        is_admin = True
        project_ids = []

        try:
            if self.auth_enabled:
                # Get session from WebSocket cookies
                cookie_header = websocket.headers.get("cookie")
                session_id = self._get_cookie_from_header(
                    cookie_header, self.cookie_name
                )

                if session_id:
                    try:
                        serializer = self._get_serializer()
                        if serializer:
                            session_data = serializer.loads(
                                session_id, max_age=self.session_ttl_seconds
                            )
                            username = session_data.get("u")

                            if (
                                username
                                and USER_REPOSITORY_AVAILABLE
                                and self._db_manager
                            ):
                                # Get user from database
                                db_session = await self._db_manager.get_session()
                                try:
                                    user = await UserRepository.get_by_username(
                                        db_session, username
                                    )
                                    if user:
                                        user_id = str(user.id)
                                        is_admin = user.role == "admin"

                                        # Get user's projects for non-admin users
                                        if not is_admin:
                                            try:
                                                from ..memory.project_repository import (
                                                    ProjectRepository,
                                                )

                                                projects = await ProjectRepository.get_user_projects(
                                                    db_session, user.id
                                                )
                                                project_ids = [
                                                    str(p.get("id"))
                                                    for p in projects
                                                    if p.get("id")
                                                ]
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to get user projects: {e}"
                                                )
                                finally:
                                    await db_session.close()
                    except Exception as e:
                        logger.debug(f"Failed to parse session for user context: {e}")
        except Exception as e:
            logger.warning(f"Error setting up user context: {e}")

        # Set os_operations context
        if OS_OPS_CONTEXT_AVAILABLE and set_current_user_context:
            set_current_user_context(user_id, is_admin, project_ids)
            logger.debug(
                f"User context set: user_id={user_id}, is_admin={is_admin}, projects={len(project_ids)}"
            )

        # Set LLM client session context (for Dreaming memory per-user isolation)
        if (
            user_id
            and self._llm_client
            and hasattr(self._llm_client, "set_session_context")
        ):
            try:
                self._llm_client.set_session_context(
                    user_id=user_id,
                    metadata={"platform": "web", "username": username or ""},
                )
                logger.debug(f"LLM session context set: user_id={user_id}")
            except Exception as e:
                logger.warning(f"Failed to set LLM session context: {e}")

    def _queue_user_message(self, data: dict):
        """Process a REST-dispatched user message without blocking the client."""
        task = asyncio.create_task(self._handle_user_message_background(data))
        self._conversation_dispatch_tasks.add(task)
        task.add_done_callback(self._conversation_dispatch_tasks.discard)

    async def _attach_project_to_conversation_if_missing(
        self, session_id: Optional[str], project_id: Optional[str]
    ) -> None:
        if not session_id or not project_id:
            return
        try:
            parsed_project_id = UUID(str(project_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid conversation project_id: %s", project_id)
            return

        try:
            from ..memory.conversation_repository import ConversationRepository

            repository = ConversationRepository()
            conversation = await repository.get_session_by_id(
                session_id, with_messages=False
            )
            if conversation and conversation.project_id is None:
                await repository.update_session(
                    session_id,
                    touch_activity=False,
                    project_id=parsed_project_id,
                )
        except Exception:
            logger.exception(
                "Failed to attach project %s to conversation %s",
                project_id,
                session_id,
            )

    async def _handle_user_message_background(self, data: dict):
        try:
            await self._handle_user_message(data)
        except Exception:
            logger.exception("Failed to process queued conversation message")

    def _conversation_control_key(self, session_id: Optional[str]) -> str:
        session_key = str(session_id or "").strip()
        return session_key or "__default__"

    def _generation_status_key(self, session_id: Optional[str]) -> str:
        return self._conversation_control_key(session_id)

    def _ensure_generation_status_store(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_conversation_generation_status"):
            self._conversation_generation_status = {}
        return self._conversation_generation_status

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
    ) -> Dict[str, Any]:
        key = self._generation_status_key(session_id)
        store = self._ensure_generation_status_store()
        previous = store.get(key, {})
        now = self._now_iso()
        payload = {
            "session_id": session_id,
            "running": running,
            "status": status,
            "message": message,
            "active_tool": active_tool,
            "agent_run_id": agent_run_id or previous.get("agent_run_id"),
            "started_at": previous.get("started_at") if previous else now,
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

        if event_type == "stream_start":
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status=str(data.get("status") or "running"),
                message=message or "応答を生成しています",
                active_tool=None,
                agent_run_id=agent_run_id,
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
            )
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
            failed = str(data.get("status") or nested_data.get("status") or "").lower() == "failed"
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="failed" if failed else "completed",
                message=message
                or (
                    "応答生成に失敗しました"
                    if failed
                    else "応答生成が完了しました"
                ),
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "stream_cancelled":
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="cancelled",
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
        image_data: Optional[dict],
        session_id: Optional[str],
        project_id: Optional[str],
        generation_profile: Optional[str],
        include_project_context: bool,
        edit_message_id: Optional[str],
        response_model: Optional[Dict[str, str]],
        client_message_id: Optional[str],
        attachments: List[Dict[str, Any]],
        attachment_context: Optional[str],
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
    ) -> None:
        if session_id:
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="queued",
                message="応答生成を開始しています",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        callback_coro = self.on_user_input(
            message,
            image_data=image_data,
            session_id=session_id,
            project_id=project_id,
            generation_profile=generation_profile,
            include_project_context=include_project_context,
            edit_message_id=edit_message_id,
            response_model=response_model,
            client_message_id=client_message_id,
            attachments=attachments,
            attachment_context=attachment_context,
            skip_user_persistence=skip_user_persistence,
            persisted_user_message_id=persisted_user_message_id,
            agent_run_id=agent_run_id,
            assistant_sender_type=assistant_sender_type,
            assistant_sender_id=assistant_sender_id,
            assistant_sender_display_name=assistant_sender_display_name,
            sender_user_id=sender_user_id,
            sender_display_name=sender_display_name,
            response_started_at_monotonic=response_started_at_monotonic,
            command_capabilities=command_capabilities,
        )
        if self.main_event_loop:
            future = asyncio.run_coroutine_threadsafe(
                callback_coro,
                self.main_event_loop,
            )
            self._register_conversation_generation_task(session_id, future)
        else:
            task = asyncio.create_task(callback_coro)
            self._register_conversation_generation_task(session_id, task)

    async def _handle_stop_generation(self, data: dict) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        key = self._conversation_control_key(session_id)
        tasks = list(self._conversation_generation_tasks.get(key, set()))
        cancelled = 0
        for task in tasks:
            if hasattr(task, "done") and task.done():
                continue
            if hasattr(task, "cancel"):
                task.cancel()
                cancelled += 1

        self._conversation_steering_queues.pop(key, None)
        current_status = self.get_conversation_generation_status(session_id)
        agent_run_id = current_status.get("agent_run_id")
        if agent_run_id:
            try:
                from ..services.agent_run_service import AgentRunService

                await AgentRunService().cancel_run(str(agent_run_id))
            except Exception:
                logger.exception("Failed to cancel agent run: %s", agent_run_id)
        event_data = {
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "status": "cancelled",
            "message": "応答生成を停止しました",
            "cancelled": cancelled,
        }
        await self.broadcast_stream_event("stream_cancelled", event_data)
        logger.info(
            "Stop generation requested: session=%s cancelled=%s", key, cancelled
        )
        return {"session_id": session_id, "cancelled": cancelled}

    async def _handle_steer_generation(self, data: dict) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        message = str(data.get("message") or data.get("instruction") or "").strip()
        if not message:
            return {"session_id": session_id, "queued": False}

        key = self._conversation_control_key(session_id)
        self._conversation_steering_queues.setdefault(key, []).append(message)
        await self.broadcast_stream_event(
            "steering_update",
            {
                "session_id": session_id,
                "status": "queued",
                "message": "追加指示を受け取りました",
            },
        )
        logger.info("Steering instruction queued: session=%s", key)
        return {"session_id": session_id, "queued": True}

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
            }
        )
        return True

    async def _handle_user_message(self, data: dict):
        """Handle user message with optional image, session_id, and project_id"""
        message = data.get("message", "").strip()
        raw_user_message = message
        raw_response_started_at = data.get("_response_started_at_monotonic")
        response_started_at_monotonic = (
            raw_response_started_at
            if isinstance(raw_response_started_at, (int, float))
            else time.monotonic()
        )
        raw_image_data = data.get("image")  # {data: base64, mimeType: str, name: str}
        image_data = None
        if isinstance(raw_image_data, str) and raw_image_data:
            image_data = {"data": raw_image_data, "mimeType": None, "name": None}
        elif isinstance(raw_image_data, dict):
            payload = raw_image_data.get("data") or raw_image_data.get("dataUrl")
            if payload:
                image_data = {
                    "data": payload,
                    "mimeType": raw_image_data.get("mimeType"),
                    "name": raw_image_data.get("name"),
                }
        session_id = data.get("session_id")  # Extract session_id from message data
        agent_run_id = data.get("agent_run_id")
        if not isinstance(agent_run_id, str) or not agent_run_id:
            agent_run_id = None
        project_id = data.get("project_id")  # Extract project_id from message data
        requested_include_project_context = data.get("include_project_context") is True
        include_project_context = effective_include_project_context(
            message=message,
            requested=requested_include_project_context,
        )
        await self._attach_project_to_conversation_if_missing(session_id, project_id)
        edit_message_id = data.get("edit_message_id")
        response_model = sanitize_response_model_selection(data.get("response_model"))
        client_message_id = data.get("client_message_id")
        if not isinstance(client_message_id, str) or not client_message_id:
            client_message_id = None
        skip_user_persistence = data.get("skip_user_persistence") is True
        persisted_user_message_id = data.get("persisted_user_message_id")
        if not isinstance(persisted_user_message_id, str) or not persisted_user_message_id:
            persisted_user_message_id = None
        attachments = sanitize_chat_attachments(data.get("attachments"))
        attachment_context = data.get("attachment_context")
        if not isinstance(attachment_context, str):
            attachment_context = None
        mentions = data.get("mentions", [])  # @mentions: [{type, id, name}]
        sender_user_id = str(data.get("_sender_user_id") or "default_user")
        sender_display_name = str(
            data.get("_sender_display_name")
            or data.get("_sender_user_id")
            or "default_user"
        )
        generation_profile = resolve_generation_profile(
            data.get("generation_profile")
        ).value
        command_capabilities = command_capabilities_for_current_turn_text(
            raw_user_message,
            sanitize_command_capabilities(data.get("command_capabilities")),
        )

        # 生成プロファイルをセッションデータに保存（同一プロセス内の参照用）
        if generation_profile:
            if not hasattr(self, "_session_generation_profiles"):
                self._session_generation_profiles = {}
            if session_id:
                self._session_generation_profiles[session_id] = generation_profile

        if not message and not image_data and not attachments and not attachment_context:
            return

        if session_id and not agent_run_id:
            try:
                from ..services.agent_run_service import AgentRunService

                agent_run = await AgentRunService().create_run(
                    session_id=session_id,
                    user_id=sender_user_id,
                    project_id=project_id,
                    trigger_message_id=persisted_user_message_id,
                    objective=message,
                    run_type="chat_turn",
                    generation_profile=generation_profile,
                    metadata={
                        "client_message_id": client_message_id,
                        "include_project_context": include_project_context,
                        "requested_include_project_context": (
                            requested_include_project_context
                        ),
                        "command_capabilities": list(command_capabilities),
                        "edit_message_id": edit_message_id,
                        "response_model": response_model,
                        "attachment_count": len(attachments),
                        "dispatch_source": "server_fallback",
                    },
                )
                agent_run_id = str(agent_run["id"])
            except Exception:
                logger.exception("Failed to create fallback agent run")

        # @メンション処理: ファイル参照の内容をメッセージに追加
        if mentions and FILE_EXPLORER_AVAILABLE:
            mention_context_parts = []
            for mention in mentions:
                m_type = mention.get("type")
                m_id = mention.get("id", "")
                m_name = mention.get("name", "")
                if m_type == "file":
                    try:
                        result = explorer_get_full_content(m_id)
                        if result.get("success"):
                            content_text = result["content"]
                            # 大きすぎるファイルは先頭だけ
                            if len(content_text) > 10000:
                                content_text = content_text[:10000] + "\n...(省略)"
                            mention_context_parts.append(
                                f"[参照ファイル: {m_name}]\n```\n{content_text}\n```"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to read mentioned file {m_id}: {e}")
                elif m_type == "task":
                    mention_context_parts.append(f"[参照タスク: {m_name} (ID: {m_id})]")
                elif m_type == "project":
                    mention_context_parts.append(
                        f"[参照プロジェクト: {m_name} (ID: {m_id})]"
                    )

            if mention_context_parts:
                message = message + "\n\n" + "\n\n".join(mention_context_parts)

        # スラッシュコマンドによるスキル明示呼び出し
        # 先頭が /skill名 のとき、LLM自動判断を待たずスキルを強制発火する。
        # 表示・永続化は生の入力のまま、LLM へ渡すメッセージのみ展開する。
        llm_message = message
        if message:
            from ..skills.slash import resolve_skill_slash_command

            skill_prompt = resolve_skill_slash_command(message)
            if skill_prompt is not None:
                llm_message = skill_prompt

        if command_capabilities:
            llm_message = build_command_capability_context(
                llm_message,
                command_capabilities,
            )

        # Set Knowledge Workspace project context for this message
        if KNOWLEDGE_PROJECT_CONTEXT_AVAILABLE and set_knowledge_project_context:
            set_knowledge_project_context(project_id)

        # Log session ID and project ID for debugging
        log_parts = [f"User message: {raw_user_message}"]
        if image_data:
            log_parts.append("(with image)")
        if session_id:
            log_parts.append(f"[session_id: {session_id}]")
        if project_id:
            log_parts.append(f"[project_id: {project_id}]")
        if include_project_context:
            log_parts.append("[project_context:on]")
        if attachments:
            log_parts.append(f"[attachments:{len(attachments)}]")
        if command_capabilities:
            log_parts.append(f"[commands:{','.join(command_capabilities)}]")
        if not session_id:
            log_parts.append("[new conversation]")
        logger.info(" ".join(log_parts))

        if session_id and await self._handle_shared_group_message(
            session_id=session_id,
            message=llm_message,
            project_id=project_id,
            sender_user_id=sender_user_id,
            sender_display_name=sender_display_name,
            generation_profile=generation_profile,
            client_message_id=client_message_id,
            attachments=attachments,
            has_image=bool(image_data),
            image_data=image_data,
        ):
            return

        # Create message entry with image info for display
        user_entry = {
            "type": "user",
            "message": raw_user_message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "session_id": session_id,
            "has_image": bool(image_data),
            "image_preview": image_data.get("data") if image_data else None,
            "client_message_id": client_message_id,
            "attachments": attachments,
            "command_capabilities": list(command_capabilities),
        }

        # Broadcast to clients
        self.manager.add_to_history(user_entry)
        await self.manager.broadcast({"type": "new_message", "data": user_entry})
        if skip_user_persistence and persisted_user_message_id:
            await self.broadcast_stream_event(
                "conversation_persisted",
                {
                    "session_id": session_id,
                    "role": "user",
                    "message_id": persisted_user_message_id,
                },
            )

        # Call user input callback with session_id and project_id
        if self.on_user_input:
            try:
                self._schedule_user_input_callback(
                    message=llm_message,
                    image_data=image_data,
                    session_id=session_id,
                    project_id=project_id,
                    generation_profile=generation_profile,
                    include_project_context=include_project_context,
                    edit_message_id=edit_message_id,
                    response_model=response_model,
                    client_message_id=client_message_id,
                    attachments=attachments,
                    attachment_context=attachment_context,
                    skip_user_persistence=skip_user_persistence,
                    persisted_user_message_id=persisted_user_message_id,
                    agent_run_id=agent_run_id,
                    sender_user_id=sender_user_id,
                    sender_display_name=sender_display_name,
                    response_started_at_monotonic=response_started_at_monotonic,
                    command_capabilities=list(command_capabilities),
                )
            except Exception as e:
                logger.error(f"Callback error: {e}")
                await self.add_assistant_message(
                    f"エラーが発生しました: {str(e)}", session_id=session_id
                )

    async def _handle_shared_group_message(
        self,
        *,
        session_id: str,
        message: str,
        project_id: Optional[str],
        sender_user_id: str,
        sender_display_name: str,
        generation_profile: Optional[str],
        client_message_id: Optional[str],
        attachments: List[Dict[str, Any]],
        has_image: bool,
        image_data: Optional[dict],
    ) -> bool:
        """Persist and fan out a shared group message if the session is shared."""
        try:
            from ..memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)
            if not session or not getattr(session, "is_group_chat", False):
                return False
            if not await repo.user_has_session_access(session_id, sender_user_id):
                logger.warning("Shared group access denied: %s", session_id)
                return True

            metadata: Dict[str, Any] = {
                "client_message_id": client_message_id,
                "attachments": attachments,
                "has_image": has_image,
            }
            if image_data:
                metadata["image_mime_type"] = image_data.get("mimeType")
                metadata["image_name"] = image_data.get("name")
            persisted = await repo.add_message(
                session_id=session_id,
                role="user",
                content=message,
                metadata={k: v for k, v in metadata.items() if v is not None},
                sender_type="user",
                sender_id=sender_user_id,
                sender_display_name=sender_display_name,
            )
            await self.broadcast_stream_event(
                "conversation_persisted",
                {
                    "session_id": session_id,
                    "role": "user",
                    "message_id": str(persisted.id),
                },
            )

            participants = await repo.get_session_participants(session_id)
            character_slugs = [
                p.participant_id
                for p in participants
                if p.participant_type == "character"
                and p.status in {"joined", "invited"}
                and p.auto_respond
            ]
            agent_ids = [
                p.participant_id
                for p in participants
                if p.participant_type == "agent"
                and p.status in {"joined", "invited"}
                and p.auto_respond
            ]

            if character_slugs:
                from ..llm.group_chat_manager import GroupChatManager

                messages = await repo.get_session_messages(session_id, limit=50)
                history = []
                for item in messages:
                    sender = item.sender_display_name or (
                        (item.message_metadata or {}).get("character_name")
                    )
                    content = item.content
                    if sender:
                        content = f"[{sender}]: {content}"
                    history.append({"role": item.role, "content": content})

                manager = GroupChatManager(
                    config=self.config,
                    character_slugs=character_slugs,
                )
                responses = await manager.generate_responses(
                    user_message=message,
                    history=history,
                    strategy="round_robin",
                )
                for response in responses:
                    saved = await repo.add_message(
                        session_id=session_id,
                        role="assistant",
                        content=response["content"],
                        metadata={"character_name": response["character_slug"]},
                        sender_type="character",
                        sender_id=response["character_slug"],
                        sender_display_name=response.get("character_name"),
                    )
                    await self.broadcast_stream_event(
                        "conversation_persisted",
                        {
                            "session_id": session_id,
                            "role": "assistant",
                            "message_id": str(saved.id),
                        },
                    )

            if agent_ids and self.on_user_input:
                self._schedule_user_input_callback(
                    message=message,
                    image_data=image_data,
                    session_id=session_id,
                    project_id=project_id,
                    generation_profile="autonomous_work",
                    include_project_context=bool(project_id),
                    edit_message_id=None,
                    response_model=None,
                    client_message_id=None,
                    attachments=attachments,
                    attachment_context=None,
                    skip_user_persistence=True,
                    assistant_sender_type="agent",
                    assistant_sender_id=agent_ids[0],
                    assistant_sender_display_name=agent_ids[0],
                    sender_user_id=sender_user_id,
                    sender_display_name=sender_display_name,
                    response_started_at_monotonic=time.monotonic(),
                )

            return True
        except Exception:
            logger.exception("Shared group message handling failed")
            await self.add_system_message(
                "グループチャットの送信処理でエラーが発生しました。"
            )
            return True

    async def _handle_clear_chat(self):
        """Handle clear chat request"""
        self.manager.clear_history()
        await self.manager.broadcast({"type": "chat_cleared"})
        logger.info("Chat history cleared")

        # Call the clear chat callback to start a new session
        if self.on_clear_chat:
            try:
                self.on_clear_chat()
            except Exception as e:
                logger.error(f"Clear chat callback error: {e}")

    def _init_external_llm_permission_manager(self):
        """Initialize the external LLM permission manager"""
        if not EXTERNAL_LLM_PERMISSION_AVAILABLE:
            return

        try:
            # Create permission manager with config
            self._external_llm_permission_manager = ExternalLLMPermissionManager(
                self.config
            )

            # Set broadcast callback
            async def broadcast_permission_request(message: dict):
                target_loop = self._permission_broadcast_loop
                current_loop = asyncio.get_running_loop()
                if (
                    target_loop
                    and target_loop.is_running()
                    and target_loop is not current_loop
                ):
                    future = asyncio.run_coroutine_threadsafe(
                        self.manager.broadcast(message),
                        target_loop,
                    )
                    await asyncio.wrap_future(future)
                    return
                await self.manager.broadcast(message)

            self._external_llm_permission_manager.set_broadcast_callback(
                broadcast_permission_request
            )

            # Register as global instance
            set_permission_manager(self._external_llm_permission_manager)

            logger.info("[WebChatServer] External LLM permission manager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize external LLM permission manager: {e}")

    async def _handle_external_llm_permission_response(self, data: dict):
        """Handle user response to external LLM permission request"""
        if not self._external_llm_permission_manager:
            logger.warning("External LLM permission manager not available")
            return

        request_id = data.get("request_id")
        approved = data.get("approved", False)

        if not request_id:
            logger.warning("Permission response missing request_id")
            return

        self._external_llm_permission_manager.handle_permission_response(
            request_id, approved
        )
        logger.info(
            f"External LLM permission response: {request_id} -> {'approved' if approved else 'denied'}"
        )

    async def _handle_external_model_prompt_response(self, data: dict):
        """Handle approval or edited prompt for an external model call."""
        if not self._external_llm_permission_manager:
            logger.warning("External LLM permission manager not available")
            return

        request_id = data.get("request_id")
        approved = bool(data.get("approved", False))
        prompt = str(data.get("prompt") or "")

        if not request_id:
            logger.warning("External model prompt response missing request_id")
            return

        self._external_llm_permission_manager.handle_external_model_prompt_response(
            request_id,
            approved,
            prompt,
        )
        logger.info(
            "External model prompt response: %s -> %s",
            request_id,
            "approved" if approved else "denied",
        )

    async def _handle_set_llm_mode(self, data: dict):
        """Handle LLM mode change from WebSocket"""
        mode = str(data.get("mode", "fast")).strip()
        state = build_llm_mode_state(self.config, client=self._llm_client)
        available_modes = state.get("available_modes") or []

        if mode not in available_modes:
            logger.warning(f"Invalid LLM mode: {mode}")
            return

        def _apply_config(key: str, next_value: Any) -> None:
            if hasattr(self.config, "save_to_file"):
                if not self.config.save_to_file(key, next_value):
                    raise RuntimeError(f"Failed to persist {key}")
            else:
                self.config.set(key, next_value)

        provider = str(state.get("provider") or "").strip()
        kind = str(state.get("kind") or "response_mode")
        if kind == "reasoning_effort":
            if provider == "codex-cli":
                _apply_config("codex_cli.reasoning_effort", mode)
            elif provider == "claude-cli":
                _apply_config("claude_cli.reasoning_effort", mode)
            elif provider == "openai":
                _apply_config("openai.reasoning_effort", mode)
            from ..llm.manager import create_llm_client

            self.set_llm_client(create_llm_client(self.config))
        elif (
            self._llm_client
            and hasattr(self._llm_client, "set_llm_mode")
        ):
            self._llm_client.set_llm_mode(mode)

        # Store mode for reference
        self._current_llm_mode = mode

        # Broadcast to all clients
        await self.manager.broadcast(
            {
                "type": "llm_mode_change",
                "data": build_llm_mode_state(self.config, client=self._llm_client),
            }
        )

        logger.info(f"LLM mode set via WebSocket: {mode}")

    async def broadcast_stream_event(self, event_type: str, data: dict):
        """Broadcast streaming events (stream_start, stream_token, tool_start, etc.)"""
        self._update_generation_status_from_stream_event(event_type, data)
        await self.manager.broadcast({"type": event_type, **data})

    async def add_assistant_message(
        self, message: str, session_id: Optional[str] = None
    ):
        """Add assistant message"""
        entry = {
            "type": "assistant",
            "message": message,
            "character": self.character_name,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "session_id": session_id,
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"Assistant: {message}")

    async def add_system_message(self, message: str):
        """Add system message"""
        entry = {
            "type": "system",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"System: {message}")

    async def add_user_message(self, message: str):
        """Add user message (for voice input)"""
        # Check for duplicate messages
        current_time = time.time()
        if (
            message == self._last_user_message
            and current_time - self._last_user_message_time < self._duplicate_threshold
        ):
            logger.info(f"Duplicate user message ignored: {message}")
            return

        # Update last message tracking
        self._last_user_message = message
        self._last_user_message_time = current_time

        entry = {
            "type": "user",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"User (voice): {message}")

    def set_user_input_callback(self, callback, event_loop=None):
        """Set user input callback"""
        self.on_user_input = callback
        self.main_event_loop = event_loop

    def set_clear_chat_callback(self, callback):
        """Set clear chat callback (called when user starts a new conversation)"""
        self.on_clear_chat = callback

    def set_llm_client_change_callback(self, callback):
        """Set callback called when the active LLM client changes."""
        self.on_llm_client_change = callback

    def set_llm_client(self, llm_client):
        """Set LLM client reference for mode management

        Args:
            llm_client: LLM client instance (SGLangClient, AgentLLMClient, etc.)
        """
        self._llm_client = llm_client
        logger.info(f"LLM client set: {type(llm_client).__name__}")
        if self.on_llm_client_change:
            try:
                self.on_llm_client_change(llm_client)
            except Exception as exc:
                logger.exception("LLM client change callback failed: %s", exc)

        # HeartbeatRunnerにもLLMクライアントとブロードキャスト関数を注入
        if self._heartbeat_runner:
            self._heartbeat_runner.set_llm_client(llm_client)
            self._heartbeat_runner.set_broadcast_fn(self.manager.broadcast)

    def _extract_mobile_ui_config(self) -> Dict[str, Any]:
        """Safely extract mobile UI configuration"""
        try:
            if hasattr(self.config, "get_mobile_ui_config"):
                return self.config.get_mobile_ui_config()
            if hasattr(self.config, "get"):
                return self.config.get("mobile_ui", {})
            if isinstance(self.config, dict):
                return self.config.get("mobile_ui", {})
        except Exception as exc:
            logger.warning(f"モバイルUI設定の取得に失敗しました: {exc}")
        return {}

    def _mobile_commands_enabled(self) -> bool:
        return bool(self.mobile_ui_config.get("enabled", True))

    def _serialize_mobile_commands(self) -> List[Dict[str, Any]]:
        commands: List[Dict[str, Any]] = []
        for cmd in self.mobile_ui_config.get("quick_commands", []):
            if not isinstance(cmd, dict):
                continue
            commands.append(
                {
                    "id": cmd.get("id"),
                    "label": cmd.get("label", "コマンド"),
                    "hint": cmd.get("hint", ""),
                    "icon": cmd.get("icon", "sparkles"),
                    "accent": cmd.get("accent", "slate"),
                    "category": cmd.get("category", "その他"),
                    "action": cmd.get("action", "send_message"),
                    "requires_confirmation": cmd.get("requires_confirmation", False),
                    "confirmation_text": cmd.get("confirmation_text", ""),
                }
            )
        return commands

    def _get_mobile_command_by_id(self, command_id: str) -> Optional[Dict[str, Any]]:
        for cmd in self.mobile_ui_config.get("quick_commands", []):
            if isinstance(cmd, dict) and cmd.get("id") == command_id:
                return cmd
        return None

    async def _execute_mobile_command(self, command_id: str) -> Dict[str, Any]:
        command = self._get_mobile_command_by_id(command_id)
        if not command:
            raise HTTPException(
                status_code=404, detail=f"Command not found: {command_id}"
            )

        action = command.get("action", "send_message")
        label = command.get("label", command_id)
        logger.info(f"Executing mobile command: %s (%s)", label, action)

        if action == "send_message":
            payload = (command.get("payload") or "").strip()
            if not payload:
                raise HTTPException(status_code=400, detail="Command payload is empty")
            await self._handle_user_message(
                {
                    "message": payload,
                    "metadata": {"source": "mobile_command", "command_id": command_id},
                }
            )
            result = "user_message_sent"
        elif action == "clear_chat":
            await self._handle_clear_chat()
            result = "chat_cleared"
        elif action == "system_message":
            payload = (command.get("payload") or "").strip()
            if payload:
                await self.add_system_message(payload)
            result = "system_message_added"
        elif action == "run_script":
            # Check if progress streaming is enabled
            stream_progress = command.get("stream_progress", False)
            if stream_progress:
                result = await self._run_script_with_progress(command, command_id)
            else:
                result = await self._run_script_command(command)
        elif action == "run_system_command":
            result = await self._run_system_command(command)
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported command action: {action}"
            )

        return {
            "success": True,
            "result": result,
            "command": {"id": command_id, "label": label, "action": action},
        }

    async def _run_script_command(self, command: Dict[str, Any]) -> str:
        """Execute a Python script with optional venv support"""
        import asyncio

        script_path = command.get("script_path", "").strip()
        if not script_path:
            raise HTTPException(
                status_code=400, detail="script_path is required for run_script action"
            )

        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Script not found: {script_path}"
            )

        # Determine Python executable
        python_executable_override = command.get("python_executable", "").strip()
        use_venv = command.get("venv_python", False)

        if python_executable_override:
            # Use specified Python executable
            python_exe = python_executable_override
        elif use_venv:
            # Use venv Python from AoiTalk project
            venv_python = (
                Path(__file__).parent.parent.parent / "venv" / "Scripts" / "python.exe"
            )
            if not venv_python.exists():
                logger.warning(
                    f"Venv python not found at {venv_python}, falling back to system python"
                )
                python_exe = "python"
            else:
                python_exe = str(venv_python)
        else:
            python_exe = "python"

        # Determine working directory
        working_dir = command.get("working_directory", "").strip()
        if working_dir:
            cwd = working_dir
        else:
            cwd = str(script_file.parent)

        logger.info(f"Executing script: {script_path} with {python_exe} in {cwd}")

        try:
            # Execute script with timeout
            process = await asyncio.create_subprocess_exec(
                python_exe,
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            # Wait with timeout (5 minutes)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=300.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Script execution timed out (5 minutes)"
                )

            # Log output
            if stdout:
                logger.info(
                    f"Script stdout: {stdout.decode('utf-8', errors='ignore')[:500]}"
                )
            if stderr:
                logger.warning(
                    f"Script stderr: {stderr.decode('utf-8', errors='ignore')[:500]}"
                )

            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}",
                )

            return "script_executed"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script: {e}")
            raise HTTPException(
                status_code=500, detail=f"Script execution failed: {str(e)}"
            )

    async def _run_script_with_progress(
        self, command: Dict[str, Any], command_id: str
    ) -> str:
        """Execute a script with real-time progress streaming via WebSocket"""
        import asyncio
        import json as json_lib

        script_path = command.get("script_path", "").strip()
        if not script_path:
            raise HTTPException(
                status_code=400, detail="script_path is required for run_script action"
            )

        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Script not found: {script_path}"
            )

        # Determine Python executable or script type
        use_venv = command.get("venv_python", False)
        python_executable_override = command.get("python_executable", "").strip()

        # Check if it's a .bat file
        is_bat = script_path.lower().endswith(".bat")

        if is_bat:
            # For .bat files, execute directly
            cmd = [str(script_file)]
        else:
            # For Python scripts
            if python_executable_override:
                # Use specified Python executable
                python_exe = python_executable_override
            elif use_venv:
                venv_python = (
                    Path(__file__).parent.parent.parent
                    / "venv"
                    / "Scripts"
                    / "python.exe"
                )
                if not venv_python.exists():
                    logger.warning(
                        f"Venv python not found at {venv_python}, falling back to system python"
                    )
                    python_exe = "python"
                else:
                    python_exe = str(venv_python)
            else:
                python_exe = "python"
            cmd = [python_exe, str(script_path)]

        logger.info(f"Executing script with progress: {' '.join(cmd)}")

        try:
            # Set environment variable to enable progress reporting
            env = os.environ.copy()
            env["REPORT_PROGRESS"] = "true"

            # Execute script
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script_file.parent),
                env=env,
            )

            # Read stdout line by line and broadcast progress
            async def read_and_broadcast():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    line_text = line.decode("utf-8", errors="ignore").strip()
                    logger.debug(f"Script output: {line_text}")

                    # Check for progress messages
                    if line_text.startswith("PROGRESS:"):
                        try:
                            # Parse JSON progress data
                            json_str = line_text[
                                9:
                            ].strip()  # Remove "PROGRESS: " prefix
                            progress_data = json_lib.loads(json_str)

                            # Broadcast to all WebSocket clients
                            await self.manager.broadcast(
                                {
                                    "type": "command_progress",
                                    "command_id": command_id,
                                    "data": progress_data,
                                }
                            )
                        except json_lib.JSONDecodeError as e:
                            logger.warning(f"Failed to parse progress JSON: {e}")

            # Start reading in background
            read_task = asyncio.create_task(read_and_broadcast())

            # Wait for process to complete (with timeout - 30 minutes for backup)
            try:
                await asyncio.wait_for(process.wait(), timeout=1800.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Script execution timed out (30 minutes)"
                )

            # Wait for reading to complete
            await read_task

            # Check return code
            if process.returncode != 0:
                # Read stderr
                stderr = await process.stderr.read()
                error_msg = stderr.decode("utf-8", errors="ignore")[:500]
                logger.error(f"Script failed: {error_msg}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}",
                )

            return "script_executed_with_progress"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script with progress: {e}")
            raise HTTPException(
                status_code=500, detail=f"Script execution failed: {str(e)}"
            )

    async def _run_system_command(self, command: Dict[str, Any]) -> str:
        """Execute a system command (Windows-only)"""
        import asyncio

        command_line = command.get("command_line", "").strip()
        if not command_line:
            raise HTTPException(
                status_code=400,
                detail="command_line is required for run_system_command action",
            )

        logger.info(f"Executing system command: {command_line}")

        try:
            # Execute command with timeout
            process = await asyncio.create_subprocess_shell(
                command_line,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            # Wait with timeout (30 seconds for system commands)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=30.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Command execution timed out (30 seconds)"
                )

            # Log output
            if stdout:
                logger.info(
                    f"Command stdout: {stdout.decode('utf-8', errors='ignore')[:500]}"
                )
            if stderr:
                logger.warning(
                    f"Command stderr: {stderr.decode('utf-8', errors='ignore')[:500]}"
                )

            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Command failed with exit code {process.returncode}",
                )

            return "system_command_executed"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute system command: {e}")
            raise HTTPException(
                status_code=500, detail=f"Command execution failed: {str(e)}"
            )

    async def _log_login_event(
        self,
        username: str,
        action: str,
        request: Request,
        success: bool = True,
        failure_reason: Optional[str] = None,
        session_duration: Optional[int] = None,
    ):
        """Log login/logout event to database

        Args:
            username: Username
            action: Action type ('login' or 'logout')
            request: FastAPI request object
            success: Whether the action was successful
            failure_reason: Reason for failure (if applicable)
            session_duration: Session duration in seconds (for logout events)
        """
        # Skip if database manager or repository is not available
        if self._db_manager is None or LoginLogRepository is None:
            return

        try:
            # Get client IP address
            ip_address = None
            if request.client:
                ip_address = request.client.host

            # Try to get IP from X-Forwarded-For header if behind proxy
            if not ip_address or ip_address == "127.0.0.1":
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    ip_address = forwarded_for.split(",")[0].strip()

            # Get user agent
            user_agent = request.headers.get("User-Agent", "")

            # Get database session
            session = await self._db_manager.get_session()
            try:
                await LoginLogRepository.create_log_entry(
                    session=session,
                    username=username,
                    action=action,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=success,
                    failure_reason=failure_reason,
                    session_duration_seconds=session_duration,
                )
            finally:
                await session.close()

            logger.info(
                f"Login event logged: {action} for {username} "
                f"(success={success}, ip={ip_address})"
            )

        except Exception as e:
            # Log error but don't fail the login/logout process
            logger.error(f"Failed to log login event: {e}")

    def _register_character_switch_callback(self):
        """キャラクター切り替え通知を登録"""
        try:
            character_manager = CharacterSwitchManager()
            character_manager.register_callback(self._on_character_switch)
            logger.info("WebChatServer: キャラクター切り替えコールバックを登録しました")
        except Exception as e:
            logger.error(
                f"WebChatServer: キャラクター切り替えコールバック登録エラー: {e}"
            )

    def _load_auth_settings(
        self,
    ) -> tuple[bool, Optional[str], Optional[str], Optional[str], int]:
        """Load authentication settings.

        DB認証に完全移行:
        - ユーザー名/パスワードは環境変数ではなくDBから取得
        - シークレットキーのみ環境変数から取得
        """
        auth_config: Dict[str, Any] = {}
        try:
            if hasattr(self.config, "get"):
                auth_config = self.config.get("web_interface.auth", {}) or {}
            elif isinstance(self.config, dict):
                auth_config = self.config.get("web_interface", {}).get("auth", {}) or {}
        except Exception as exc:
            logger.warning(f"WebUI 認証設定の取得に失敗しました: {exc}")

        # シークレットキーは環境変数から取得（セッション署名用）
        env_secret = os.getenv("AOITALK_WEB_AUTH_SECRET")
        secret = (env_secret or auth_config.get("secret") or "").strip() or None

        ttl_minutes = auth_config.get("session_ttl_minutes", 1440)
        try:
            ttl_minutes = int(ttl_minutes)
        except (TypeError, ValueError):
            ttl_minutes = 1440

        # DB認証が利用可能かチェック
        if USER_REPOSITORY_AVAILABLE and self._db_manager is not None:
            # DB認証モード: シークレット必須
            enabled = True
            if not secret:
                raise ValueError(
                    "WebUI 認証が有効ですが、AOITALK_WEB_AUTH_SECRET が設定されていません"
                )
            logger.info("WebUI 認証: DBベース認証が有効です")
            # username/password は None（DBから取得するため）
            return enabled, None, None, secret, max(60, ttl_minutes * 60)
        else:
            # DB利用不可の場合は認証無効
            logger.warning("WebUI 認証: UserRepositoryが利用不可のため、認証は無効です")
            return False, None, None, None, max(60, ttl_minutes * 60)

    async def _verify_credentials_async(
        self, username: str, password: str
    ) -> Optional[Any]:
        """Verify credentials against database (async).

        Returns User object if successful, None otherwise.
        """
        if not self.auth_enabled:
            return True

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            logger.warning("UserRepository not available for authentication")
            return None

        try:
            session = await self._db_manager.get_session()
            try:
                user = await UserRepository.authenticate(
                    session=session, username=username, password=password
                )
                return user
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return None

    def _verify_credentials(self, username: str, password: str) -> bool:
        """Verify credentials (sync wrapper for backward compatibility).

        Note: This is a sync wrapper. For full async support, use login endpoint directly.
        """
        if not self.auth_enabled:
            return True

        # 同期コンテキストでは非同期認証を実行できないため、
        # ログインエンドポイントでは直接 _verify_credentials_async を使用
        logger.warning(
            "_verify_credentials called in sync context - use _verify_credentials_async"
        )
        return False

    def _get_serializer(self) -> Optional[URLSafeTimedSerializer]:
        if not self.auth_secret:
            return None
        return URLSafeTimedSerializer(self.auth_secret, salt="aoitalk-webui-session-v2")

    def _sign_session(self, username: str) -> str:
        serializer = self._get_serializer()
        if not serializer:
            raise ValueError("WebUI 認証シークレットが未設定です")
        return serializer.dumps({"u": username})

    def _verify_session(self, session_id: str) -> bool:
        if not self.auth_enabled:
            return True
        serializer = self._get_serializer()
        if not serializer or not session_id:
            return False
        try:
            serializer.loads(session_id, max_age=self.session_ttl_seconds)
            return True
        except (BadSignature, SignatureExpired):
            return False

    def _decode_next_session_cookie(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """Decode the Next.js JWT session cookie used by the Web UI."""
        if not token:
            return None
        nextauth_secret = os.getenv("NEXTAUTH_SECRET")
        if not nextauth_secret:
            logger.error("NEXTAUTH_SECRET is required to verify Next.js session cookies")
            return None
        try:
            return jwt.decode(
                token,
                nextauth_secret,
                algorithms=["HS256"],
            )
        except Exception:
            return None

    def _verify_cookie_auth(self, request: Request) -> bool:
        session_id = request.cookies.get(self.cookie_name)
        if self._verify_session(session_id):
            return True

        legacy_session_id = request.cookies.get(self.legacy_cookie_name)
        if self._verify_session(legacy_session_id):
            return True

        return self._decode_next_session_cookie(
            request.cookies.get(self.next_cookie_name)
        ) is not None

    def _get_cookie_from_header(
        self, cookie_header: Optional[str], name: str
    ) -> Optional[str]:
        if not cookie_header:
            return None
        parts = cookie_header.split(";")
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return None

    def _is_request_authenticated(self, request: Request) -> bool:
        if not self.auth_enabled:
            return True
        # Next.js内部プロキシからのリクエストを許可
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            return bool(internal_key)  # 空文字の場合はFalse
        # Bearer token認証（モバイルアプリ / サーバー間アクセス）
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                return self._get_username_from_long_lived_token(token) is not None
            # JWTアクセストークン（モバイルアプリ用）
            if AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                return payload is not None
        # Cookie認証（Webブラウザ用）
        return self._verify_cookie_auth(request)

    def _enforce_cookie_auth(self, request: Request) -> None:
        if not self.auth_enabled:
            return
        if not self._is_request_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _get_username_from_long_lived_token(self, token: str) -> Optional[str]:
        """長期APIトークンを検証し、有効ならユーザー名を返す（同期）。

        同期セッションを使うため、同期の認証依存（スレッドプール実行）から呼ぶ。
        """
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None
        try:
            from ..memory.models import User

            with self._db_manager.get_sync_session() as session:
                record = ApiTokenRepository.verify_token_sync(session, token)
                if record is None:
                    return None
                user = session.get(User, record.user_id)
                if user is None or not user.is_active:
                    return None
                return user.username
        except Exception as exc:
            logger.warning(f"Long-lived token verification failed: {exc}")
            return None

    async def _get_user_info_from_long_lived_token(
        self, token: str
    ) -> Optional[Dict[str, Any]]:
        """長期APIトークンを検証し、有効ならユーザー情報dictを返す（非同期）。"""
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None
        if not USER_REPOSITORY_AVAILABLE:
            return None
        try:
            session = await self._db_manager.get_session()
            try:
                record = await ApiTokenRepository.verify_token(session, token)
                if record is None:
                    return None
                user = await UserRepository.get_by_id(session, record.user_id)
                if user is None or not user.is_active:
                    return None
                return user.to_dict()
            finally:
                await session.close()
        except Exception as exc:
            logger.error(f"Long-lived token user lookup failed: {exc}")
            return None

    def _get_username_from_request(self, request: Request) -> Optional[str]:
        """Extract username from session cookie or Bearer token.

        Returns:
            Username string if session is valid, None otherwise
        """
        if not self.auth_enabled:
            return None

        # Next.js内部プロキシからのx-forwarded-userヘッダー（internal-auth検証済みの場合のみ信頼）
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            forwarded_user = request.headers.get("x-forwarded-user")
            if forwarded_user:
                return forwarded_user

        # Bearer tokenから取得（モバイルアプリ / サーバー間アクセス）
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                username = self._get_username_from_long_lived_token(token)
                if username:
                    return username
            elif AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                if payload:
                    return payload.username

        # Cookieから取得（FastAPI直接ログイン/旧Cookie）
        session_id = request.cookies.get(self.cookie_name)
        if not session_id:
            session_id = request.cookies.get(self.legacy_cookie_name)
        if not session_id:
            return None

        serializer = self._get_serializer()
        if not serializer:
            return None

        try:
            session_data = serializer.loads(
                session_id, max_age=self.session_ttl_seconds
            )
            return session_data.get("u")
        except (BadSignature, SignatureExpired):
            return None

    def _get_next_user_id_from_request(self, request: Request) -> Optional[str]:
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            forwarded_user_id = request.headers.get("x-forwarded-user-id")
            if forwarded_user_id:
                return str(forwarded_user_id)

        payload = self._decode_next_session_cookie(
            request.cookies.get(self.next_cookie_name)
        )
        if not payload:
            return None
        user_id = payload.get("sub")
        return str(user_id) if user_id else None

    async def _is_admin_user(self, request: Request) -> bool:
        """Check if current user has admin role.

        Returns:
            True if user is admin, False otherwise
        """
        username = self._get_username_from_request(request)
        if not username:
            return False

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return False

        try:
            session = await self._db_manager.get_session()
            try:
                user = await UserRepository.get_by_username(session, username)
                if user and user.role == "admin":
                    return True
                return False
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to check admin status: {e}")
            return False

    async def _get_user_info_from_request(
        self, request: Request
    ) -> Optional[Dict[str, Any]]:
        """Get full user info from request session.

        Returns:
            User info dict with id, username, role, etc. or None if not authenticated
        """
        # 長期APIトークン（サーバー間アクセス）は非同期経路で先に解決し、
        # 同期DBアクセスでイベントループをブロックしないようにする。
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:]
            if bearer.startswith(LONG_LIVED_TOKEN_PREFIX):
                return await self._get_user_info_from_long_lived_token(bearer)

        next_user_id = self._get_next_user_id_from_request(request)
        username = None if next_user_id else self._get_username_from_request(request)
        if not username and not next_user_id:
            return None

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None

        try:
            session = await self._db_manager.get_session()
            try:
                if next_user_id:
                    from uuid import UUID

                    user = await UserRepository.get_by_id(session, UUID(next_user_id))
                else:
                    user = await UserRepository.get_by_username(session, username)
                if user:
                    return user.to_dict()
                return None
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
            return None

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

        router = create_skill_router(require_auth=require_auth)
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

    def _set_session_cookie(
        self, response: JSONResponse, session_id: str, secure: bool
    ) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=self.session_ttl_seconds,
        )

    def _authorize_websocket(self, websocket: WebSocket) -> bool:
        if not self.auth_enabled:
            return True
        # Bearer token認証（モバイルアプリ用 — クエリパラメータ）
        token = websocket.query_params.get("token")
        if token and AUTH_SERVICE_AVAILABLE:
            payload = get_auth_service().verify_token(token)
            if payload is not None:
                return True
        # Cookie認証（Webブラウザ用）
        cookie_header = websocket.headers.get("cookie")
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        if self._verify_session(session_id):
            return True
        legacy_session_id = self._get_cookie_from_header(
            cookie_header, self.legacy_cookie_name
        )
        if self._verify_session(legacy_session_id):
            return True
        next_session = self._get_cookie_from_header(cookie_header, self.next_cookie_name)
        return self._decode_next_session_cookie(next_session) is not None

    def _verify_api_key(self, request: Request) -> bool:
        """Verify Bearer token for crawler API access"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]  # Remove "Bearer " prefix
        expected_key = os.getenv("CRAWLER_API_KEY")
        return token == expected_key if expected_key else False

    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """キャラクター切り替え時のコールバック"""
        try:
            logger.info(
                f"WebChatServer: キャラクター切り替えを受信 - {self.character_name} -> {character_name}"
            )
            old_character = self.character_name
            self.character_name = character_name

            # WebSocketで接続中のクライアントに通知
            if hasattr(self, "manager") and self.manager:
                try:
                    # 実行中のイベントループを取得
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.manager.broadcast(
                            {
                                "type": "character_switch",
                                "data": {
                                    "old_character": old_character,
                                    "new_character": character_name,
                                    "yaml_filename": yaml_filename,
                                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                                },
                            }
                        )
                    )
                except RuntimeError:
                    # イベントループが実行されていない場合
                    logger.warning(
                        "WebChatServer: イベントループが実行されていないため、WebSocket通知をスキップします"
                    )

            logger.info(
                f"WebChatServer: キャラクター名を更新しました - {character_name}"
            )

        except Exception as e:
            logger.error(f"WebChatServer: キャラクター切り替え処理エラー: {e}")

    def set_voice_recognition_ready(self, ready: bool):
        """Set voice recognition ready state"""
        self.voice_recognition_ready = ready
        # Broadcast to all clients
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast(
                        {
                            "type": "voice_status_change",
                            "data": {
                                "ready": ready,
                                "rms": self.current_rms,
                                "recording": self.is_recording,
                            },
                        }
                    )
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass

    def update_rms(self, rms: float):
        """Update microphone RMS level"""
        self.current_rms = rms
        # Broadcast to all clients
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast({"type": "rms_update", "data": {"rms": rms}})
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass

    def set_recording_state(self, recording: bool):
        """Set recording state"""
        self.is_recording = recording
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast(
                        {
                            "type": "voice_status_change",
                            "data": {
                                "ready": self.voice_recognition_ready,
                                "rms": self.current_rms,
                                "recording": recording,
                            },
                        }
                    )
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass

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
