#!/usr/bin/env python3
"""
FastAPI + WebSocket server for AoiTalk Web Interface
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from ..mode_switch import ModeSwitchError, mode_switch_manager

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
    from .auth_service import AuthService, get_auth_service, TokenPayload
    AUTH_SERVICE_AVAILABLE = True
except ImportError:
    AUTH_SERVICE_AVAILABLE = False
    AuthService = None
    get_auth_service = None
    TokenPayload = None

# Import WebSession for multi-user support
try:
    from ..session.web_session import WebSession
    WEB_SESSION_AVAILABLE = True
except ImportError:
    WEB_SESSION_AVAILABLE = False
    WebSession = None

# Import CrawlerStatusChecker
try:
    from ..crawler_status import CrawlerStatusChecker
except ImportError:
    CrawlerStatusChecker = None

# Import media browser
try:
    from ..tools.media_browser import (
        get_media_config,
        list_folder_contents,
        get_file_path,
        get_media_mime_type,
        load_bookmarks,
        add_bookmark,
        remove_bookmark,
        get_video_thumbnail_path,
    )
    MEDIA_BROWSER_AVAILABLE = True
except ImportError:
    MEDIA_BROWSER_AVAILABLE = False
    get_media_config = None
    list_folder_contents = None
    get_file_path = None
    get_media_mime_type = None
    load_bookmarks = None
    add_bookmark = None
    remove_bookmark = None

# Import file explorer service (replaces old user_files)
try:
    from ..tools.file_explorer import (
        list_directory as explorer_list_directory,
        create_directory as explorer_create_directory,
        upload_file as explorer_upload_file,
        download_file as explorer_download_file,
        rename_item as explorer_rename_item,
        move_item as explorer_move_item,
        copy_item as explorer_copy_item,
        delete_item as explorer_delete_item,
        get_file_info as explorer_get_file_info,
        get_preview as explorer_get_preview,
        get_directory_tree as explorer_get_directory_tree,
        # Bookmark functions
        get_bookmarks as explorer_get_bookmarks,
        add_bookmark as explorer_add_bookmark,
        remove_bookmark as explorer_remove_bookmark,
    )
    FILE_EXPLORER_AVAILABLE = True
except ImportError:
    FILE_EXPLORER_AVAILABLE = False
    explorer_list_directory = None
    explorer_create_directory = None
    explorer_upload_file = None
    explorer_download_file = None
    explorer_rename_item = None
    explorer_move_item = None
    explorer_copy_item = None
    explorer_delete_item = None
    explorer_get_file_info = None
    explorer_get_preview = None
    explorer_get_directory_tree = None
    explorer_get_bookmarks = None
    explorer_add_bookmark = None
    explorer_remove_bookmark = None

# Import storage context service
try:
    from ..tools.file_explorer.storage_context import (
        StorageContextType,
        get_context_root,
        ensure_user_storage,
        ensure_project_storage,
        get_available_contexts_for_user,
        calculate_storage_usage,
    )
    STORAGE_CONTEXT_AVAILABLE = True
except ImportError:
    STORAGE_CONTEXT_AVAILABLE = False
    StorageContextType = None
    get_context_root = None
    ensure_user_storage = None
    ensure_project_storage = None
    get_available_contexts_for_user = None
    calculate_storage_usage = None

# Import document converter (office_reader)
try:
    from ..tools.documents.office_reader import (
        convert_office_bytes_to_markdown,
        is_supported as is_office_file_supported,
        SUPPORTED_EXTENSIONS as OFFICE_SUPPORTED_EXTENSIONS,
    )
    OFFICE_READER_AVAILABLE = True
except ImportError:
    OFFICE_READER_AVAILABLE = False
    convert_office_bytes_to_markdown = None
    is_office_file_supported = None
    OFFICE_SUPPORTED_EXTENSIONS = set()

# Import external LLM permission manager
try:
    from ..tools.external_llm_permission import (
        ExternalLLMPermissionManager,
        set_permission_manager,
    )
    EXTERNAL_LLM_PERMISSION_AVAILABLE = True
except ImportError:
    EXTERNAL_LLM_PERMISSION_AVAILABLE = False

# Import os_operations user context functions
try:
    from ..tools.os_operations.tools import (
        set_current_user_context,
        clear_user_context,
    )
    OS_OPS_CONTEXT_AVAILABLE = True
except ImportError:
    OS_OPS_CONTEXT_AVAILABLE = False
    set_current_user_context = None
    clear_user_context = None
    ExternalLLMPermissionManager = None
    set_permission_manager = None

# Import RAG manager
try:
    from ..rag import RagManager, get_rag_manager
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False

# Import RAG project context
try:
    from ..tools.rag import set_current_project_context as set_rag_project_context
    RAG_PROJECT_CONTEXT_AVAILABLE = True
except ImportError:
    set_rag_project_context = None
    RAG_PROJECT_CONTEXT_AVAILABLE = False
    RagManager = None
    get_rag_manager = None

# Import project routes
try:
    from .project_routes import create_project_router
    PROJECT_ROUTES_AVAILABLE = True
except ImportError:
    PROJECT_ROUTES_AVAILABLE = False
    create_project_router = None

# Import RAG collection routes
try:
    from .rag_collection_routes import create_rag_collection_router
    RAG_COLLECTION_ROUTES_AVAILABLE = True
except ImportError:
    RAG_COLLECTION_ROUTES_AVAILABLE = False
    create_rag_collection_router = None

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

# Import skill routes
try:
    from .skill_routes import create_skill_router
    SKILL_ROUTES_AVAILABLE = True
except ImportError:
    SKILL_ROUTES_AVAILABLE = False
    create_skill_router = None

# Import heartbeat routes
try:
    from .heartbeat_routes import create_heartbeat_router
    HEARTBEAT_ROUTES_AVAILABLE = True
except ImportError:
    HEARTBEAT_ROUTES_AVAILABLE = False
    create_heartbeat_router = None

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Data models
class ChatMessage(BaseModel):
    type: str  # 'user', 'assistant', 'system'
    message: str
    timestamp: str
    character: Optional[str] = None

class UserMessage(BaseModel):
    message: str

class VoiceStatus(BaseModel):
    ready: bool
    rms: float
    recording: bool


class MobileCommandRequest(BaseModel):
    command_id: str


class ModeSwitchPayload(BaseModel):
    target_mode: str


class LoginPayload(BaseModel):
    username: str
    password: str


class CreateUserPayload(BaseModel):
    """Payload for creating a new user (admin only)"""
    username: str
    password: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: str = 'user'  # 'admin' or 'user'


class UpdateUserPayload(BaseModel):
    """Payload for updating user details"""
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    preferred_character: Optional[str] = None


class ChangePasswordPayload(BaseModel):
    """Payload for changing password"""
    current_password: Optional[str] = None  # Required for non-admin users
    new_password: str


class CrawlerStatusReport(BaseModel):
    """クローラーからのステータスレポート
    
    クローラーは追加のフィールド（processed_servers, processed_channels等）を
    送信する場合があるため、extra='allow'で受け入れる。
    """
    model_config = {"extra": "allow"}
    
    name: str  # クローラー名（例: "VideoCrawler", "DiscordCrawler"）
    status: str  # "running", "idle", "error"
    details: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


class DocumentContent(BaseModel):
    content: str


class SettingsPayload(BaseModel):
    """Payload for updating a configuration setting"""
    key: str  # e.g., "tts.speed_adjustment"
    value: Any
    persist: bool = True  # Whether to save to config.yaml


class RagIndexRequest(BaseModel):
    """Payload for RAG index creation"""
    path: str  # Path to file or directory to index
    clear: bool = False  # Clear existing index before indexing


class ConnectionManager:
    """Manages WebSocket connections with per-user session support"""
    
    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()
        # Legacy shared chat history (for backward compatibility when auth disabled)
        self.chat_history: List[dict] = []
        self.max_history = 100
        
        # Per-user sessions for multi-user support
        # Maps user_id -> WebSession (when WEB_SESSION_AVAILABLE)
        self.user_sessions: Dict[str, Any] = {}
        
    async def connect(self, websocket: WebSocket) -> None:
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"Client connected. Total connections: {len(self.active_connections)}")
        
        # Send chat history to new client
        await websocket.send_json({
            "type": "chat_history",
            "data": self.chat_history
        })
        
    def disconnect(self, websocket: WebSocket) -> None:
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"Client disconnected. Total connections: {len(self.active_connections)}")
        
    async def broadcast(self, message: dict) -> None:
        """Send message to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending message: {e}")
                disconnected.append(connection)
                
        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)
            
    def add_to_history(self, entry: dict) -> None:
        """Add message to chat history"""
        self.chat_history.append(entry)
        if len(self.chat_history) > self.max_history:
            self.chat_history = self.chat_history[-self.max_history:]
            
    def clear_history(self) -> None:
        """Clear chat history"""
        self.chat_history.clear()

class WebChatServer:
    """FastAPI-based web chat server"""
    
    def __init__(self, config, character_name: str):
        self.config = config
        self.character_name = character_name
        
        # Store reference to self for lifespan to use (set before app creation)
        self._db_manager_for_lifespan = None
        
        # Heartbeat runner reference
        self._heartbeat_runner = None
        try:
            from ..heartbeat.runner import get_heartbeat_runner
            heartbeat_config = config.get('heartbeat', {}) if hasattr(config, 'get') else {}
            if heartbeat_config.get('enabled', True):
                self._heartbeat_runner = get_heartbeat_runner()
        except Exception as e:
            logger.warning(f"Heartbeat runner initialization skipped: {e}")

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
            yield
            # Shutdown
            if self._heartbeat_runner:
                try:
                    await self._heartbeat_runner.stop()
                except Exception as e:
                    logger.error(f"Heartbeat runner stop failed: {e}")
        
        self.app = FastAPI(title="AoiTalk Web Interface", lifespan=lifespan)
        
        # Debug logging
        logger.info(f"WebChatServer initialized with character: {character_name}")
        logger.info(f"Config type: {type(config)}")
        if hasattr(config, 'config'):
            logger.info(f"Config has 'config' attribute")
            
        # キャラクター切り替え通知の登録
        self._register_character_switch_callback()
        
        # Add CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Database manager for login logging (must be before auth settings)
        self._db_manager = None
        if get_database_manager is not None:
            try:
                self._db_manager = get_database_manager()
            except Exception as e:
                logger.warning(f"Failed to initialize database manager for login logging: {e}")
        
        # Auth settings (depends on _db_manager for DB auth)
        (
            self.auth_enabled,
            self.auth_user,
            self.auth_pass,
            self.auth_secret,
            self.session_ttl_seconds,
        ) = self._load_auth_settings()
        self.cookie_name = "aoitalk_session"

        # Connection manager
        self.manager = ConnectionManager()
        
        # Callbacks
        self.on_user_input = None
        self.on_clear_chat = None  # Callback for clear chat events
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
        if EXTERNAL_LLM_PERMISSION_AVAILABLE:
            self._init_external_llm_permission_manager()

        # LLM client reference (will be set by terminal/voice mode)
        self._llm_client = None
        self._current_llm_mode = "fast"  # 'fast' or 'thinking'


        # Setup routes
        self._setup_routes()
        
        # Register project routes if available
        if PROJECT_ROUTES_AVAILABLE and create_project_router:
            self._register_project_routes()

        # Register RAG collection routes if available
        if RAG_COLLECTION_ROUTES_AVAILABLE and create_rag_collection_router:
            self._register_rag_collection_routes()

        # Register git routes if available
        if GIT_ROUTES_AVAILABLE and create_git_router:
            self._register_git_routes()
        
        # Register conversation routes if available
        if CONVERSATION_ROUTES_AVAILABLE and create_conversation_router:
            self._register_conversation_routes()

        # Register skill routes if available
        if SKILL_ROUTES_AVAILABLE and create_skill_router:
            self._register_skill_routes()

        # Register heartbeat routes if available
        if HEARTBEAT_ROUTES_AVAILABLE and create_heartbeat_router:
            self._register_heartbeat_routes()

    async def _on_startup(self):
        """Startup event handler - ensures admin user exists"""
        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            logger.info("Admin initialization skipped: UserRepository not available")
            return
        
        try:
            session = await self._db_manager.get_session()
            try:
                initial_admin_password = os.getenv("AOITALK_INITIAL_ADMIN_PASSWORD") or None
                created_password = await UserRepository.ensure_admin_exists(
                    session,
                    default_password=initial_admin_password,
                )
                if created_password:
                    logger.warning(
                        f"⚠️  初期管理者を作成しました: admin / {created_password}\n"
                        "⚠️  セキュリティのため、ログイン後すぐにパスワードを変更してください！"
                    )
                else:
                    logger.info("Admin user already exists")
            except Exception as e:
                logger.error(f"Failed to ensure admin exists: {e}")
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get database session for admin initialization: {e}")
        
    def _setup_routes(self):
        """Setup API routes"""

        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)
        
        @self.app.get("/")
        async def get_index():
            """Serve main page"""
            html_path = Path(__file__).parent.parent / "web" / "templates" / "index.html"
            if html_path.exists():
                return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
            else:
                # Fallback if template not found
                return HTMLResponse(content="<h1>AoiTalk Web Interface</h1><p>Template not found</p>")
        
        @self.app.get("/api/modes")
        async def get_modes(_: None = Depends(require_auth)):
            """Expose current/next mode info"""
            return JSONResponse(mode_switch_manager.get_status())

        @self.app.post("/api/mode-switch")
        async def switch_mode(payload: ModeSwitchPayload, _: None = Depends(require_auth)):
            try:
                result = await mode_switch_manager.request_switch(
                    payload.target_mode,
                    source="web_ui",
                )
                await self.add_system_message("🔁 モード切り替え要求を受け付けました。数秒後に再起動します。")
                return JSONResponse({"status": "accepted", **result})
            except ModeSwitchError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @self.app.get("/api/config")
        async def get_config(_: None = Depends(require_auth)):
            """Get configuration"""
            # Handle both dict and Config object
            if hasattr(self.config, 'config'):
                # Config object
                config_dict = self.config.config
                llm_model = config_dict.get('llm_model', 'unknown')
                llm_provider = config_dict.get('llm_provider', 'unknown')
                speech_config = config_dict.get('speech_recognition', {})
            else:
                # Plain dict
                llm_model = self.config.get('llm_model', 'unknown')
                llm_provider = self.config.get('llm_provider', 'unknown')
                speech_config = self.config.get('speech_recognition', {})
            
            # エージェントツール系の場合はプロバイダー名のみを表示
            # (gemini-cli, codex, claude codeなどはモデル名ではなくツール名を表示)
            agent_tool_providers = ['gemini-cli', 'codex', 'claude']
            if llm_provider in agent_tool_providers:
                # エージェントツール系の場合はモデル名をプロバイダー名に置き換え
                llm_model = llm_provider
            
            speech_engine = speech_config.get('current_engine', 'unknown')
            speech_model = speech_config.get('engines', {}).get(
                speech_engine, {}
            ).get('model', 'unknown')
            
            # セッションIDを取得
            from ..utils.app_session import get_session_id
            session_id = get_session_id()
            
            # Debug logging
            logger.info(f"API Config Response - LLM: {llm_model} ({llm_provider}), ASR: {speech_engine} ({speech_model})")
            
            return JSONResponse({
                "character_name": self.character_name,
                "max_history": self.manager.max_history,
                "llm_model": llm_model,
                "llm_provider": llm_provider,
                "asr_engine": speech_engine,
                "asr_model": speech_model,
                "session_id": session_id
            })
            
        @self.app.get("/api/voice_status")
        async def get_voice_status(_: None = Depends(require_auth)):
            """Get voice recognition status"""
            return JSONResponse({
                "ready": self.voice_recognition_ready,
                "rms": self.current_rms,
                "recording": self.is_recording
            })
            
        @self.app.get("/api/characters")
        async def get_characters(_: None = Depends(require_auth)):
            """Get list of available characters"""
            try:
                characters = self.config.get_available_characters()
                return JSONResponse({
                    "characters": characters,
                    "current": self.character_name
                })
            except Exception as e:
                logger.error(f"Failed to get characters: {e}")
                return JSONResponse({
                    "characters": [],
                    "current": self.character_name,
                    "error": str(e)
                }, status_code=500)

        # ── Settings API Endpoints ──────────────────────────────────────────
        # Allowed settings that can be modified via WebUI
        ALLOWED_SETTINGS = {
            "external_llm.auto_approve": {"type": "bool"},
            "rag.enabled": {"type": "bool"},
            "reasoning.enabled": {"type": "bool"},
            "reasoning.display_mode": {"type": "enum", "values": ["silent", "progress", "detailed", "debug"]},
            # Agent/Tool toggles
            "agents.filesystem.enabled": {"type": "bool"},
            "mcp_enabled": {"type": "bool"},
            "agents.spotify.enabled": {"type": "bool"},
            "clickup_sync.enabled": {"type": "bool"},
            "spotify.enabled": {"type": "bool"},
        }

        @self.app.get("/api/settings")
        async def get_settings(_: None = Depends(require_auth)):
            """Get configurable settings"""
            try:
                settings = {
                    "external_llm": {
                        "auto_approve": self.config.get("external_llm.auto_approve", True)
                    },
                    "rag": {
                        "enabled": self.config.get("rag.enabled", False)
                    },
                    "reasoning": {
                        "enabled": self.config.get("reasoning.enabled", False),
                        "display_mode": self.config.get("reasoning.display_mode", "progress")
                    },
                    "agents": {
                        "filesystem": {
                            "enabled": self.config.get("agents.filesystem.enabled", True)
                        },
                        "clickup_mcp": {
                            "enabled": self.config.get("mcp_enabled", True)
                        },
                        "spotify": {
                            "enabled": self.config.get("agents.spotify.enabled", True)
                        }
                    },
                    "clickup_sync": {
                        "enabled": self.config.get("clickup_sync.enabled", False)
                    },
                    "spotify": {
                        "enabled": self.config.get("spotify.enabled", True)
                    }
                }
                return JSONResponse({"settings": settings, "schema": ALLOWED_SETTINGS})
            except Exception as e:
                logger.error(f"Failed to get settings: {e}")
                raise HTTPException(status_code=500, detail=str(e))


        @self.app.patch("/api/settings")
        async def update_setting(payload: SettingsPayload, _: None = Depends(require_auth)):
            """Update a configuration setting"""
            key = payload.key
            value = payload.value
            persist = payload.persist
            
            # Validate the key is allowed
            if key not in ALLOWED_SETTINGS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Setting '{key}' is not configurable via WebUI"
                )
            
            # Validate value type and constraints
            setting_schema = ALLOWED_SETTINGS[key]
            try:
                if setting_schema["type"] == "bool":
                    if not isinstance(value, bool):
                        value = str(value).lower() in ("true", "1", "yes")
                elif setting_schema["type"] == "float":
                    value = float(value)
                    if "min" in setting_schema and value < setting_schema["min"]:
                        raise ValueError(f"Value must be >= {setting_schema['min']}")
                    if "max" in setting_schema and value > setting_schema["max"]:
                        raise ValueError(f"Value must be <= {setting_schema['max']}")
                elif setting_schema["type"] == "enum":
                    if value not in setting_schema["values"]:
                        raise ValueError(f"Value must be one of: {setting_schema['values']}")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            
            # Apply the setting
            try:
                if persist:
                    # Save to both memory and file
                    success = self.config.save_to_file(key, value)
                    if not success:
                        raise HTTPException(
                            status_code=500,
                            detail="Failed to persist setting to config.yaml"
                        )
                else:
                    # Only update in memory
                    self.config.set(key, value)
                
                logger.info(f"Setting updated: {key} = {value} (persist={persist})")
                return JSONResponse({
                    "success": True,
                    "key": key,
                    "value": value,
                    "persisted": persist
                })
            except Exception as e:
                logger.error(f"Failed to update setting: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # ── LLM Mode API Endpoints ──────────────────────────────────────────
        @self.app.get("/api/llm/mode")
        async def get_llm_mode(_: None = Depends(require_auth)):
            """Get current LLM response mode"""
            mode = "fast"  # default
            if self._llm_client and hasattr(self._llm_client, 'get_llm_mode'):
                mode = self._llm_client.get_llm_mode()
            return JSONResponse({"mode": mode})

        @self.app.post("/api/llm/mode")
        async def set_llm_mode(request: Request, _: None = Depends(require_auth)):
            """Set LLM response mode (fast or thinking)"""
            try:
                body = await request.json()
                mode = body.get("mode", "fast")
                
                if mode not in ["fast", "thinking"]:
                    raise HTTPException(
                        status_code=400, 
                        detail="Invalid mode. Use 'fast' or 'thinking'"
                    )
                
                # Apply to LLM client if available
                if self._llm_client and hasattr(self._llm_client, 'set_llm_mode'):
                    self._llm_client.set_llm_mode(mode)
                    logger.info(f"LLM mode set to: {mode}")
                
                # Store mode for reference
                self._current_llm_mode = mode
                
                # Broadcast mode change to all WebSocket clients
                await self.manager.broadcast({
                    "type": "llm_mode_change",
                    "data": {"mode": mode}
                })
                
                return JSONResponse({
                    "success": True,
                    "mode": mode,
                    "message": f"LLMモードを{'思考モード' if mode == 'thinking' else '高速モード'}に設定しました"
                })
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to set LLM mode: {e}")
                raise HTTPException(status_code=500, detail=str(e))


        @self.app.get("/api/crawler/status")
        async def get_crawler_status(_: None = Depends(require_auth)):
            """Get status of external crawlers (DiscordCrawler, EventMonitor, VideoCrawler, HydrusClient)"""
            if CrawlerStatusChecker is None:
                raise HTTPException(
                    status_code=503,
                    detail="Crawler status checking is not available"
                )
            
            try:
                checker = CrawlerStatusChecker()
                status_data = {}
                
                # Check all crawlers: health check only (Pull), details from Push cache
                for crawler_name in ["DiscordCrawler", "EventMonitor", "HydrusClient", "VideoCrawler"]:
                    # VideoCrawler and HydrusClient are special: need detailed Pull status
                    # VideoCrawler: external service, need HTTP call anyway
                    # HydrusClient: doesn't send Push updates
                    if crawler_name == "VideoCrawler":
                        detailed_status = await checker.get_video_crawler_detailed_status()
                        status_data[crawler_name] = detailed_status
                    elif crawler_name == "HydrusClient":
                        detailed_status = await checker.get_hydrus_client_detailed_status()
                        status_data[crawler_name] = detailed_status
                    else:
                        is_alive = await checker.check_alive(crawler_name)
                        
                        if is_alive and crawler_name in self._crawler_status_cache:
                            # Alive + Push cache available → use detailed status
                            status_data[crawler_name] = self._crawler_status_cache[crawler_name].copy()
                            status_data[crawler_name]["is_alive"] = True
                        elif is_alive:
                            # Alive but no Push data → just indicate running
                            status_data[crawler_name] = {
                                "status": "running",
                                "details": None,
                                "is_alive": True
                            }
                        else:
                            # Dead/stopped
                            status_data[crawler_name] = {
                                "status": "stopped",
                                "is_alive": False
                            }
                
                # Convert dict to array format expected by frontend and add crawler names
                crawlers_array = []
                for crawler_name, crawler_data in status_data.items():
                    crawler_entry = crawler_data.copy()
                    crawler_entry["name"] = crawler_name
                    # Ensure 'type' field exists for frontend badge display
                    if "type" not in crawler_entry:
                        crawler_entry["type"] = "cloud" if crawler_name == "VideoCrawler" else "local"
                    crawlers_array.append(crawler_entry)
                
                return JSONResponse({
                    "crawlers": crawlers_array,
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"Failed to get crawler status: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to get crawler status: {e}")

        @self.app.post("/api/crawler/report")
        async def receive_crawler_status(report: CrawlerStatusReport, request: Request):
            """Receive status push from crawlers"""
            if not self._verify_api_key(request):
                raise HTTPException(status_code=401, detail="Invalid API key")
            
            # Build details from explicit details field and any extra fields
            # Crawlers may send fields like processed_servers, processed_channels 
            # as top-level fields rather than inside 'details'
            details = report.details.copy() if report.details else {}
            
            # Merge extra fields from Pydantic model (extra='allow' captures these)
            if hasattr(report, 'model_extra') and report.model_extra:
                details.update(report.model_extra)
            elif hasattr(report, '__pydantic_extra__') and report.__pydantic_extra__:
                details.update(report.__pydantic_extra__)
            
            # Store status in cache
            self._crawler_status_cache[report.name] = {
                "status": report.status,
                "details": details if details else None,
                "error": report.error,
                "received_at": datetime.now().isoformat()
            }
            
            logger.info(f"Received crawler status push: {report.name} - {report.status} (details: {bool(details)})")
            
            # Broadcast to WebSocket clients
            await self.manager.broadcast({
                "type": "crawler_status_update",
                "data": {
                    "name": report.name,
                    "status": report.status,
                    "details": details if details else None,
                    "error": report.error
                }
            })
            
            return JSONResponse({"accepted": True})

        @self.app.post("/api/crawler/restart/{crawler_name}")
        async def restart_crawler(crawler_name: str, _: None = Depends(require_auth)):
            """Restart a crawler"""
            if CrawlerStatusChecker is None:
                raise HTTPException(
                    status_code=503,
                    detail="Crawler control is not available"
                )
            
            try:
                checker = CrawlerStatusChecker()
                
                # Route to appropriate restart method
                if crawler_name.lower() == "videocrawler":
                    result = await checker.restart_video_crawler()
                elif crawler_name.lower() == "discordcrawler":
                    result = await checker.restart_discord_crawler()
                elif crawler_name.lower() == "eventmonitor":
                    result = await checker.restart_event_monitor()
                elif crawler_name.lower() == "hydrusclient":
                    result = await checker.launch_hydrus_client()
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Restart not supported for {crawler_name}"
                    )
                
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Failed to restart crawler: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to restart crawler: {e}")

        @self.app.post("/api/crawler/stop/{crawler_name}")
        async def stop_crawler(crawler_name: str, _: None = Depends(require_auth)):
            """Stop a running crawler"""
            if CrawlerStatusChecker is None:
                raise HTTPException(
                    status_code=503,
                    detail="Crawler control is not available"
                )
            
            try:
                checker = CrawlerStatusChecker()
                
                # Route to appropriate stop method
                if crawler_name.lower() == "discordcrawler":
                    result = await checker.stop_discord_crawler()
                elif crawler_name.lower() == "eventmonitor":
                    result = await checker.stop_event_monitor()
                elif crawler_name.lower() == "hydrusclient":
                    result = await checker.stop_hydrus_client()
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Stop not supported for {crawler_name}"
                    )
                
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Failed to stop crawler: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to stop crawler: {e}")

        @self.app.get("/api/mobile/commands")
        async def get_mobile_commands(_: None = Depends(require_auth)):
            """Return mobile quick command metadata"""
            if not self._mobile_commands_enabled():
                return JSONResponse({
                    "enabled": False,
                    "commands": []
                })

            return JSONResponse({
                "enabled": True,
                "default_view": self.mobile_ui_config.get('default_view', 'chat'),
                "commands": self._serialize_mobile_commands()
            })

        @self.app.post("/api/mobile/commands/run")
        async def run_mobile_command(request: MobileCommandRequest, _: None = Depends(require_auth)):
            """Execute a configured mobile command"""
            if not self._mobile_commands_enabled():
                raise HTTPException(status_code=403, detail="Mobile commands are disabled")

            result = await self._execute_mobile_command(request.command_id)
            return JSONResponse(result)

        # ── Media Browser API Endpoints ──────────────────────────────────
        @self.app.get("/api/media/config")
        async def get_media_browser_config(_: None = Depends(require_auth)):
            """Get media browser configuration (root path and bookmarks)"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            try:
                result = get_media_config()
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Failed to get media config: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/media/browse")
        async def browse_media_folder(
            path: str = "",
            _: None = Depends(require_auth)
        ):
            """List contents of a directory (both images and videos)"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            try:
                result = list_folder_contents(path)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to browse folder")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to browse media folder: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/media/file")
        async def serve_media_file(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Serve a media file by absolute path"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            file_path = get_file_path(path)
            if file_path is None:
                raise HTTPException(status_code=404, detail="File not found")
            
            mime_type = get_media_mime_type(file_path)
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                filename=file_path.name
            )
        
        @self.app.get("/api/media/video-thumbnail")
        async def serve_video_thumbnail(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Serve a video thumbnail (generated via FFmpeg)"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            thumbnail_path = get_video_thumbnail_path(path)
            if thumbnail_path is None:
                raise HTTPException(status_code=404, detail="Video thumbnail not available")
            
            return FileResponse(
                path=str(thumbnail_path),
                media_type="image/jpeg",
                filename=thumbnail_path.name
            )
        
        @self.app.get("/api/media/bookmarks")
        async def get_media_bookmarks(_: None = Depends(require_auth)):
            """Get all bookmarks"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            try:
                bookmarks = load_bookmarks()
                return JSONResponse({"bookmarks": bookmarks})
            except Exception as e:
                logger.error(f"Failed to get bookmarks: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        class BookmarkPayload(BaseModel):
            name: str
            path: str
            icon: str = "📁"
        
        @self.app.post("/api/media/bookmarks")
        async def add_media_bookmark(
            payload: BookmarkPayload,
            _: None = Depends(require_auth)
        ):
            """Add a new bookmark"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            try:
                result = add_bookmark(payload.name, payload.path, payload.icon)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to add bookmark")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to add bookmark: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        class BookmarkDeletePayload(BaseModel):
            path: str
        
        @self.app.delete("/api/media/bookmarks")
        async def remove_media_bookmark(
            payload: BookmarkDeletePayload,
            _: None = Depends(require_auth)
        ):
            """Remove a bookmark by path"""
            if not MEDIA_BROWSER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Media browser is not available"
                )
            
            try:
                result = remove_bookmark(payload.path)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to remove bookmark")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to remove bookmark: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # ── File Explorer API Endpoints ────────────────────────────────────
        # Replaces old user-files API with full file explorer functionality
        
        class ExplorerMkdirPayload(BaseModel):
            path: str = ""  # Parent directory path
            name: str  # New directory name
        
        class ExplorerRenamePayload(BaseModel):
            path: str
            new_name: str
        
        class ExplorerMovePayload(BaseModel):
            src: str
            dest: str
        
        class ExplorerCopyPayload(BaseModel):
            src: str
            dest: str
        
        @self.app.get("/api/explorer/tree")
        async def get_explorer_tree(
            root: str = "",
            _: None = Depends(require_auth)
        ):
            """Get directory tree structure"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_get_directory_tree(root_path=root)
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Failed to get directory tree: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/explorer/list")
        async def explorer_list(
            request: Request,
            path: str = "",
            _: None = Depends(require_auth)
        ):
            """List directory contents"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                # Check if user is admin to allow browsing outside user_files
                is_admin = await self._is_admin_user(request)
                
                result = explorer_list_directory(path, is_admin=is_admin)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to list directory")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to list directory: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/explorer/mkdir")
        async def explorer_mkdir(
            payload: ExplorerMkdirPayload,
            _: None = Depends(require_auth)
        ):
            """Create a new directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_create_directory(payload.path, payload.name)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to create directory")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to create directory: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/explorer/upload")
        async def explorer_upload(
            file: UploadFile = File(...),
            path: str = "",
            _: None = Depends(require_auth)
        ):
            """Upload a file to the specified directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                file_bytes = await file.read()
                filename = file.filename or "unnamed_file"
                
                result = explorer_upload_file(path, filename, file_bytes)
                
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to upload file")
                    )
                
                logger.info(f"Uploaded file: {filename} to {path or 'root'} ({result.get('size_bytes', 0)} bytes)")
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to upload file: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/explorer/download")
        async def explorer_download(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Download a file"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            content, filename, mime_type = explorer_download_file(path)
            if content is None:
                raise HTTPException(status_code=404, detail="File not found")
            
            from fastapi.responses import Response
            from urllib.parse import quote
            
            # RFC 5987: Use filename* for non-ASCII filenames
            # Also include ASCII-safe filename for compatibility
            ascii_filename = filename.encode('ascii', 'replace').decode('ascii')
            encoded_filename = quote(filename, safe='')
            content_disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
            
            return Response(
                content=content,
                media_type=mime_type,
                headers={"Content-Disposition": content_disposition}
            )
        
        @self.app.get("/api/explorer/info")
        async def explorer_info(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Get file/directory info"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_get_file_info(path)
                if not result.get("success"):
                    raise HTTPException(status_code=404, detail=result.get("error", "Not found"))
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to get file info: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/explorer/preview")
        async def explorer_preview(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Get file preview (text content, image data, etc.)"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_get_preview(path)
                if not result.get("success"):
                    raise HTTPException(status_code=404, detail=result.get("error", "Not found"))
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to get preview: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/explorer/rename")
        async def explorer_rename(
            payload: ExplorerRenamePayload,
            _: None = Depends(require_auth)
        ):
            """Rename a file or directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_rename_item(payload.path, payload.new_name)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to rename")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to rename: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/explorer/move")
        async def explorer_move(
            payload: ExplorerMovePayload,
            _: None = Depends(require_auth)
        ):
            """Move a file or directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_move_item(payload.src, payload.dest)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to move")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to move: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/explorer/copy")
        async def explorer_copy(
            payload: ExplorerCopyPayload,
            _: None = Depends(require_auth)
        ):
            """Copy a file or directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_copy_item(payload.src, payload.dest)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to copy")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to copy: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.delete("/api/explorer/delete")
        async def explorer_delete(
            path: str,
            _: None = Depends(require_auth)
        ):
            """Delete a file or directory"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_delete_item(path)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to delete")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to delete: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        # ── File Explorer Bookmark Endpoints ────────────────────────────────
        
        @self.app.get("/api/explorer/bookmarks")
        async def explorer_bookmarks_list(
            _: None = Depends(require_auth)
        ):
            """Get all bookmarks"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_get_bookmarks()
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Failed to get bookmarks: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        class ExplorerBookmarkPayload(BaseModel):
            name: str
            path: str
            icon: str = "📁"
        
        @self.app.post("/api/explorer/bookmarks")
        async def explorer_bookmarks_add(
            payload: ExplorerBookmarkPayload,
            _: None = Depends(require_auth)
        ):
            """Add a new bookmark"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_add_bookmark(payload.name, payload.path, payload.icon)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to add bookmark")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to add bookmark: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        class ExplorerBookmarkDeletePayload(BaseModel):
            path: str
        
        @self.app.delete("/api/explorer/bookmarks")
        async def explorer_bookmarks_delete(
            payload: ExplorerBookmarkDeletePayload,
            _: None = Depends(require_auth)
        ):
            """Remove a bookmark"""
            if not FILE_EXPLORER_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="File explorer is not available"
                )
            
            try:
                result = explorer_remove_bookmark(payload.path)
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to remove bookmark")
                    )
                return JSONResponse(result)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to remove bookmark: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # ── Document Upload API Endpoints ─────────────────────────────────────
        # Convert Office files (docx, xlsx, pptx, pdf) to Markdown
        # Also supports plain text/data files directly
        
        # Supported text file extensions (read directly without conversion)
        TEXT_FILE_EXTENSIONS = {
            # テキスト
            '.txt', '.log', '.md', '.markdown', '.rst', '.text',
            # データ/設定ファイル
            '.csv', '.tsv', '.json', '.jsonl', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
            # Web関連
            '.html', '.htm', '.css',
            # コード（主要言語）
            '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.c', '.cpp', '.h', '.hpp',
            '.cs', '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.bat', '.ps1',
            '.sql', '.r', '.swift', '.kt', '.scala', '.lua',
        }
        
        def is_text_file(filename: str) -> bool:
            """Check if file is a plain text file"""
            ext = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            return ext in TEXT_FILE_EXTENSIONS
        
        @self.app.post("/api/documents/upload")
        async def upload_document(
            file: UploadFile = File(...),
            _: None = Depends(require_auth)
        ):
            """Upload and convert document to text/markdown"""
            filename = file.filename or "unnamed_file"
            
            try:
                # Read file content
                file_bytes = await file.read()
                
                # Check if it's a plain text file
                if is_text_file(filename):
                    # Decode text file directly
                    try:
                        content = file_bytes.decode('utf-8')
                    except UnicodeDecodeError:
                        # Try other encodings
                        try:
                            content = file_bytes.decode('shift_jis')
                        except UnicodeDecodeError:
                            content = file_bytes.decode('utf-8', errors='replace')
                    
                    logger.info(f"Text file read: {filename} ({len(file_bytes)} bytes)")
                    
                    return JSONResponse({
                        "success": True,
                        "filename": filename,
                        "content": content,
                        "size_bytes": len(file_bytes)
                    })
                
                # Check if it's an Office file
                if OFFICE_READER_AVAILABLE and is_office_file_supported(filename):
                    # Convert using office_reader
                    result = convert_office_bytes_to_markdown(file_bytes, filename)
                    
                    if not result.get("success"):
                        raise HTTPException(
                            status_code=400,
                            detail=result.get("error", "ファイル変換に失敗しました")
                        )
                    
                    logger.info(f"Document converted: {filename} ({len(file_bytes)} bytes)")
                    
                    return JSONResponse({
                        "success": True,
                        "filename": filename,
                        "content": result.get("content", ""),
                        "size_bytes": len(file_bytes)
                    })
                
                # Unsupported file type
                all_supported = list(TEXT_FILE_EXTENSIONS)
                if OFFICE_READER_AVAILABLE:
                    all_supported.extend(OFFICE_SUPPORTED_EXTENSIONS)
                supported_list = ", ".join(sorted(all_supported))
                raise HTTPException(
                    status_code=400,
                    detail=f"対応していないファイル形式です。対応形式: {supported_list}"
                )
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to process document: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # ── Storage Context API Endpoints ────────────────────────────────────

        @self.app.get("/api/storage/contexts")
        async def get_storage_contexts(
            request: Request,
            _: None = Depends(require_auth)
        ):
            """Get available storage contexts for the current user"""
            if not STORAGE_CONTEXT_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Storage context service not available"
                )
            
            user_info = await self._get_user_info_from_request(request)
            if not user_info:
                raise HTTPException(status_code=401, detail="Not authenticated")
            
            try:
                from uuid import UUID
                user_id = UUID(user_info["id"])
                
                # Check if user is admin
                is_admin = await self._is_admin_user(request)
                
                # Ensure user storage exists
                ensure_user_storage(user_id)
                
                # Get user's projects if ProjectRepository is available
                projects = []
                if self._db_manager and PROJECT_ROUTES_AVAILABLE:
                    from ..memory.project_repository import ProjectRepository
                    session = await self._db_manager.get_session()
                    try:
                        projects = await ProjectRepository.get_user_projects(session, user_id)
                    finally:
                        await session.close()
                
                contexts = get_available_contexts_for_user(user_id, projects)
                
                return JSONResponse({
                    "success": True,
                    "contexts": contexts,
                    "current_context": {
                        "type": "personal",
                        "id": str(user_id)
                    },
                    "is_admin": is_admin
                })
            except Exception as e:
                logger.error(f"Failed to get storage contexts: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/storage/usage")
        async def get_storage_usage(
            request: Request,
            context_type: str = "personal",
            context_id: Optional[str] = None,
            _: None = Depends(require_auth)
        ):
            """Get storage usage for a context"""
            if not STORAGE_CONTEXT_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Storage context service not available"
                )
            
            user_info = await self._get_user_info_from_request(request)
            if not user_info:
                raise HTTPException(status_code=401, detail="Not authenticated")
            
            try:
                from uuid import UUID
                user_id = UUID(user_info["id"])
                
                ctx_type = StorageContextType(context_type)
                ctx_id = UUID(context_id) if context_id else None
                
                root_path, valid = get_context_root(ctx_type, ctx_id, user_id)
                if not valid:
                    raise HTTPException(status_code=400, detail="Invalid storage context")
                
                usage = calculate_storage_usage(root_path)
                
                return JSONResponse({
                    "success": True,
                    "context_type": context_type,
                    "context_id": context_id,
                    "usage": usage
                })
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid context type: {e}")
            except Exception as e:
                logger.error(f"Failed to get storage usage: {e}")
                raise HTTPException(status_code=500, detail=str(e))


        @self.app.post("/api/character/{character_name}")
        async def switch_character(character_name: str, _: None = Depends(require_auth)):
            """Switch to a different character"""
            try:
                # Get character switch manager
                character_manager = CharacterSwitchManager()
                
                # Try to get character config to validate it exists
                char_config = self.config.get_character_config(character_name)
                
                # Switch character
                success = character_manager.switch_character(
                    character_name,
                    character_name.replace(' ', '_').lower()  # Convert to yaml filename format
                )
                
                if success:
                    # Update server's character name
                    self.character_name = character_name
                    
                    return JSONResponse({
                        "success": True,
                        "character": character_name,
                        "message": f"Switched to {character_name}"
                    })
                else:
                    raise HTTPException(status_code=500, detail="Failed to switch character")
                    
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=f"Character not found: {character_name}")
            except Exception as e:
                logger.error(f"Failed to switch character: {e}")
                raise HTTPException(status_code=500, detail=str(e))
            
        @self.app.get("/api/auth/status")
        async def auth_status(request: Request):
            """Check whether the request is authenticated"""
            return JSONResponse({"authenticated": self._is_request_authenticated(request)})

        @self.app.post("/api/auth/login")
        async def login(payload: LoginPayload, request: Request):
            """Login and set session cookie (DB-based authentication)"""
            if not self.auth_enabled:
                return JSONResponse({"authenticated": True})

            # Verify credentials against database
            user = await self._verify_credentials_async(payload.username, payload.password)
            
            if not user:
                # Log failed login attempt
                await self._log_login_event(
                    username=payload.username,
                    action='login',
                    request=request,
                    success=False,
                    failure_reason='invalid_credentials'
                )
                raise HTTPException(status_code=401, detail="Invalid credentials")

            # Check if user is active
            if hasattr(user, 'is_active') and not user.is_active:
                await self._log_login_event(
                    username=payload.username,
                    action='login',
                    request=request,
                    success=False,
                    failure_reason='account_disabled'
                )
                raise HTTPException(status_code=401, detail="Account is disabled")

            # Store login time for session duration calculation
            self._login_sessions[payload.username] = datetime.utcnow()
            
            # Log successful login
            await self._log_login_event(
                username=payload.username,
                action='login',
                request=request,
                success=True
            )
            
            session_id = self._sign_session(payload.username)
            
            # Build response with user info
            response_data = {
                "authenticated": True,
                "user": {
                    "username": payload.username,
                    "role": getattr(user, 'role', 'user') if hasattr(user, 'role') else 'user',
                    "display_name": getattr(user, 'display_name', None) if hasattr(user, 'display_name') else None,
                    "password_reset_required": getattr(user, 'is_password_reset_required', False) if hasattr(user, 'is_password_reset_required') else False
                }
            }
            
            response = JSONResponse(response_data)
            self._set_session_cookie(response, session_id, request.url.scheme == "https")
            return response

        @self.app.post("/api/auth/logout")
        async def logout(request: Request):
            """Logout and clear session cookie"""
            if not self.auth_enabled:
                return JSONResponse({"authenticated": False})

            # Try to get username from session to log logout
            username = None
            session_duration = None
            
            try:
                session_id = request.cookies.get(self.cookie_name)
                if session_id:
                    serializer = self._get_serializer()
                    if serializer:
                        session_data = serializer.loads(session_id, max_age=self.session_ttl_seconds)
                        username = session_data.get('u')
                        
                        # Calculate session duration
                        if username and username in self._login_sessions:
                            login_time = self._login_sessions[username]
                            session_duration = int((datetime.utcnow() - login_time).total_seconds())
                            # Clean up session tracking
                            del self._login_sessions[username]
            except Exception as e:
                logger.debug(f"Could not extract username from session for logout logging: {e}")
            
            # Log logout event
            if username:
                await self._log_login_event(
                    username=username,
                    action='logout',
                    request=request,
                    success=True,
                    session_duration=session_duration
                )

            response = JSONResponse({"authenticated": False})
            response.delete_cookie(self.cookie_name)
            return response

        @self.app.get("/api/auth/login-history")
        async def get_login_history(
            request: Request,
            limit: int = 100,
            offset: int = 0,
            action: Optional[str] = None,
            username: Optional[str] = None,
            success: Optional[bool] = None,
            _: None = Depends(require_auth)
        ):
            """Get login/logout history with filtering and pagination"""
            if self._db_manager is None or LoginLogRepository is None:
                raise HTTPException(
                    status_code=503,
                    detail="Login history logging is not available (database not configured)"
                )
            
            try:
                # Get database session
                session = await self._db_manager.get_session()
                try:
                    logs, total_count = await LoginLogRepository.get_login_history(
                        session=session,
                        limit=min(limit, 500),  # Cap at 500 records
                        offset=offset,
                        username=username,
                        action=action,
                        success=success
                    )
                    
                    return JSONResponse({
                        "logs": [log.to_dict() for log in logs],
                        "total_count": total_count,
                        "limit": limit,
                        "offset": offset
                    })
                finally:
                    await session.close()
            except Exception as e:
                logger.error(f"Failed to get login history: {e}")
                raise HTTPException(status_code=500, detail="Failed to retrieve login history")

        @self.app.delete("/api/auth/login-history/clear")
        async def clear_login_history(
            request: Request,
            before_date: Optional[str] = None,
            _: None = Depends(require_auth)
        ):
            """Clear login history logs
            
            Args:
                before_date: ISO format date string. If provided, delete logs before this date.
                            If not provided, delete all logs.
            """
            if self._db_manager is None or LoginLogRepository is None:
                raise HTTPException(
                    status_code=503,
                    detail="Login history logging is not available (database not configured)"
                )
            
            try:
                session = await self._db_manager.get_session()
                try:
                    if before_date:
                        # Parse date string
                        try:
                            before_dt = datetime.fromisoformat(before_date.replace('Z', '+00:00'))
                        except ValueError:
                            raise HTTPException(status_code=400, detail="Invalid date format. Use ISO format.")
                        
                        deleted_count = await LoginLogRepository.delete_logs_before(
                            session=session,
                            before_date=before_dt
                        )
                        message = f"Deleted {deleted_count} log entries before {before_date}"
                    else:
                        # Clear all logs
                        deleted_count = await LoginLogRepository.clear_all_logs(session=session)
                        message = f"Deleted all {deleted_count} log entries"
                    
                    logger.info(f"Login history cleared: {message}")
                    return JSONResponse({
                        "deleted_count": deleted_count,
                        "message": message
                    })
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to clear login history: {e}")
                raise HTTPException(status_code=500, detail="Failed to clear login history")

        # ── User Management API Endpoints (Admin only) ────────────────────────
        
        async def require_admin(request: Request) -> None:
            """Require admin role for the endpoint"""
            self._enforce_cookie_auth(request)
            
            # Check if user has admin role
            is_admin = await self._is_admin_user(request)
            if not is_admin:
                raise HTTPException(
                    status_code=403,
                    detail="Administrator privileges required"
                )
        
        @self.app.get("/api/users")
        async def list_users(
            request: Request,
            limit: int = 100,
            offset: int = 0,
            include_inactive: bool = False,
            _: None = Depends(require_admin)
        ):
            """List all users (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                session = await self._db_manager.get_session()
                try:
                    users, total_count = await UserRepository.list_users(
                        session=session,
                        limit=min(limit, 500),
                        offset=offset,
                        include_inactive=include_inactive
                    )
                    
                    return JSONResponse({
                        "users": [user.to_dict() for user in users],
                        "total_count": total_count,
                        "limit": limit,
                        "offset": offset
                    })
                finally:
                    await session.close()
            except Exception as e:
                logger.error(f"Failed to list users: {e}")
                raise HTTPException(status_code=500, detail="Failed to list users")
        
        @self.app.post("/api/users")
        async def create_user(
            payload: CreateUserPayload,
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Create a new user (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                session = await self._db_manager.get_session()
                try:
                    user = await UserRepository.create_user(
                        session=session,
                        username=payload.username,
                        password=payload.password,
                        email=payload.email,
                        display_name=payload.display_name,
                        role=payload.role,
                        is_password_reset_required=True  # Always require password change
                    )
                    
                    logger.info(f"User created: {payload.username} (by admin)")
                    return JSONResponse({
                        "success": True,
                        "user": user.to_dict(),
                        "message": f"User '{payload.username}' created successfully"
                    })
                finally:
                    await session.close()
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                logger.error(f"Failed to create user: {e}")
                raise HTTPException(status_code=500, detail="Failed to create user")

        # ── User CSV Import/Export Endpoints (Admin only) ─────────────────────
        # NOTE: These must be registered BEFORE /api/users/{user_id} routes
        # to prevent FastAPI from matching "export"/"import" as a user_id.

        @self.app.get("/api/users/export")
        async def export_users_csv(
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Export all users as CSV (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )

            try:
                import csv
                import io
                from datetime import datetime as dt

                session = await self._db_manager.get_session()
                try:
                    users, _ = await UserRepository.list_users(
                        session=session,
                        limit=10000,
                        include_inactive=True
                    )

                    # Create CSV in memory (with BOM for Excel compatibility)
                    output = io.StringIO()
                    output.write('\ufeff')  # UTF-8 BOM
                    writer = csv.writer(output)

                    # Header row
                    writer.writerow(['username', 'password', 'email', 'display_name', 'role', 'is_active'])

                    # Data rows (password column is empty for security)
                    for user in users:
                        writer.writerow([
                            user.username,
                            '',  # Password is not exported
                            user.email or '',
                            user.display_name or '',
                            user.role or 'user',
                            'true' if user.is_active else 'false'
                        ])

                    csv_content = output.getvalue()
                    output.close()

                    # Generate filename with date
                    filename = f"users_{dt.now().strftime('%Y%m%d')}.csv"

                    from fastapi.responses import Response
                    return Response(
                        content=csv_content,
                        media_type="text/csv",
                        headers={
                            "Content-Disposition": f"attachment; filename={filename}"
                        }
                    )
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to export users: {e}")
                raise HTTPException(status_code=500, detail="Failed to export users")

        @self.app.post("/api/users/import")
        async def import_users_csv(
            request: Request,
            file: UploadFile = File(...),
            _: None = Depends(require_admin)
        ):
            """Import users from CSV (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )

            if not file.filename or not file.filename.endswith('.csv'):
                raise HTTPException(status_code=400, detail="CSVファイルをアップロードしてください")

            try:
                import csv
                import io

                # Read file content
                content = await file.read()
                try:
                    text_content = content.decode('utf-8')
                except UnicodeDecodeError:
                    # Try Shift-JIS for Japanese Excel files
                    text_content = content.decode('shift-jis')

                # Remove BOM if present
                if text_content.startswith('\ufeff'):
                    text_content = text_content[1:]

                reader = csv.DictReader(io.StringIO(text_content))

                # Validate headers
                required_headers = {'username'}
                if not required_headers.issubset(set(reader.fieldnames or [])):
                    raise HTTPException(status_code=400, detail="CSVにusernameカラムが必要です")

                results = {
                    'created': 0,
                    'updated': 0,
                    'skipped': 0,
                    'errors': []
                }

                session = await self._db_manager.get_session()
                try:
                    for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
                        username = row.get('username', '').strip()
                        if not username:
                            results['skipped'] += 1
                            continue

                        password = row.get('password', '').strip()
                        email = row.get('email', '').strip() or None
                        display_name = row.get('display_name', '').strip() or None
                        role = row.get('role', 'user').strip().lower()
                        is_active_str = row.get('is_active', 'true').strip().lower()
                        is_active = is_active_str in ('true', '1', 'yes', 'on')

                        # Validate role
                        if role not in ('admin', 'user'):
                            role = 'user'

                        try:
                            # Check if user exists
                            existing_user = await UserRepository.get_by_username(session, username)

                            if existing_user:
                                # Update existing user
                                update_data = {
                                    'email': email,
                                    'display_name': display_name,
                                    'role': role,
                                    'is_active': is_active
                                }
                                await UserRepository.update_user(
                                    session=session,
                                    user_id=existing_user.id,
                                    **update_data
                                )

                                # Update password if provided
                                if password:
                                    await UserRepository.update_password(
                                        session=session,
                                        user_id=existing_user.id,
                                        new_password=password,
                                        clear_reset_flag=False
                                    )

                                results['updated'] += 1
                            else:
                                # Create new user (password required)
                                if not password:
                                    results['errors'].append(f"行{row_num}: 新規ユーザー '{username}' にはパスワードが必要です")
                                    results['skipped'] += 1
                                    continue

                                await UserRepository.create_user(
                                    session=session,
                                    username=username,
                                    password=password,
                                    email=email,
                                    display_name=display_name,
                                    role=role,
                                    is_password_reset_required=True
                                )
                                results['created'] += 1
                        except Exception as e:
                            results['errors'].append(f"行{row_num}: {username} - {str(e)}")

                    logger.info(f"CSV import completed: created={results['created']}, updated={results['updated']}, skipped={results['skipped']}, errors={len(results['errors'])}")

                    return JSONResponse({
                        "success": True,
                        "created": results['created'],
                        "updated": results['updated'],
                        "skipped": results['skipped'],
                        "errors": results['errors'][:10],  # Limit error messages
                        "message": f"インポート完了: {results['created']}件作成, {results['updated']}件更新"
                    })
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to import users: {e}")
                raise HTTPException(status_code=500, detail=f"インポートに失敗しました: {str(e)}")

        @self.app.get("/api/users/{user_id}")
        async def get_user(
            user_id: str,
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Get user details (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                from uuid import UUID as PyUUID
                uuid_obj = PyUUID(user_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID format")
            
            try:
                session = await self._db_manager.get_session()
                try:
                    user = await UserRepository.get_by_id(session, uuid_obj)
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    return JSONResponse({"user": user.to_dict()})
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to get user: {e}")
                raise HTTPException(status_code=500, detail="Failed to get user")
        
        @self.app.patch("/api/users/{user_id}")
        async def update_user(
            user_id: str,
            payload: UpdateUserPayload,
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Update user details (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                from uuid import UUID as PyUUID
                uuid_obj = PyUUID(user_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID format")
            
            # Build update dict from non-None values
            update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
            
            if not update_data:
                raise HTTPException(status_code=400, detail="No update data provided")
            
            try:
                session = await self._db_manager.get_session()
                try:
                    user = await UserRepository.update_user(
                        session=session,
                        user_id=uuid_obj,
                        **update_data
                    )
                    
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    logger.info(f"User updated: {user.username}")
                    return JSONResponse({
                        "success": True,
                        "user": user.to_dict(),
                        "message": f"User '{user.username}' updated successfully"
                    })
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to update user: {e}")
                raise HTTPException(status_code=500, detail="Failed to update user")
        
        @self.app.delete("/api/users/{user_id}")
        async def delete_user(
            user_id: str,
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Delete a user (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                from uuid import UUID as PyUUID
                uuid_obj = PyUUID(user_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID format")
            
            try:
                session = await self._db_manager.get_session()
                try:
                    # Prevent deleting the last admin
                    user = await UserRepository.get_by_id(session, uuid_obj)
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    if user.role == 'admin':
                        admin_count = await UserRepository.count_admins(session)
                        if admin_count <= 1:
                            raise HTTPException(
                                status_code=400,
                                detail="Cannot delete the last admin user"
                            )
                    
                    username = user.username
                    deleted = await UserRepository.delete_user(session, uuid_obj)
                    
                    if not deleted:
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    logger.info(f"User deleted: {username}")
                    return JSONResponse({
                        "success": True,
                        "message": f"User '{username}' deleted successfully"
                    })
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to delete user: {e}")
                raise HTTPException(status_code=500, detail="Failed to delete user")
        
        @self.app.post("/api/users/{user_id}/change-password")
        async def admin_change_password(
            user_id: str,
            payload: ChangePasswordPayload,
            request: Request,
            _: None = Depends(require_admin)
        ):
            """Change user password (admin only)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="User management is not available (database not configured)"
                )
            
            try:
                from uuid import UUID as PyUUID
                uuid_obj = PyUUID(user_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID format")
            
            try:
                session = await self._db_manager.get_session()
                try:
                    success = await UserRepository.update_password(
                        session=session,
                        user_id=uuid_obj,
                        new_password=payload.new_password,
                        clear_reset_flag=True
                    )
                    
                    if not success:
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    logger.info(f"Password changed for user ID: {user_id}")
                    return JSONResponse({
                        "success": True,
                        "message": "Password changed successfully"
                    })
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to change password: {e}")
                raise HTTPException(status_code=500, detail="Failed to change password")
        
        @self.app.post("/api/auth/change-password")
        async def self_change_password(
            payload: ChangePasswordPayload,
            request: Request,
            _: None = Depends(require_auth)
        ):
            """Change own password (requires current password)"""
            if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="Password change is not available (database not configured)"
                )
            
            # Get current user from session
            try:
                session_id = request.cookies.get(self.cookie_name)
                if not session_id:
                    raise HTTPException(status_code=401, detail="Not authenticated")
                
                serializer = self._get_serializer()
                if not serializer:
                    raise HTTPException(status_code=500, detail="Auth not configured")
                
                session_data = serializer.loads(session_id, max_age=self.session_ttl_seconds)
                username = session_data.get('u')
                
                if not username:
                    raise HTTPException(status_code=401, detail="Invalid session")
            except Exception as e:
                logger.error(f"Failed to get session: {e}")
                raise HTTPException(status_code=401, detail="Invalid session")
            
            # Require current password for non-admin self-change
            if not payload.current_password:
                raise HTTPException(
                    status_code=400,
                    detail="Current password is required"
                )
            
            try:
                db_session = await self._db_manager.get_session()
                try:
                    # Verify current password
                    user = await UserRepository.authenticate(
                        session=db_session,
                        username=username,
                        password=payload.current_password
                    )
                    
                    if not user:
                        raise HTTPException(status_code=401, detail="Current password is incorrect")
                    
                    # Update password
                    success = await UserRepository.update_password(
                        session=db_session,
                        user_id=user.id,
                        new_password=payload.new_password,
                        clear_reset_flag=True
                    )
                    
                    if not success:
                        raise HTTPException(status_code=500, detail="Failed to update password")
                    
                    logger.info(f"User changed own password: {username}")
                    return JSONResponse({
                        "success": True,
                        "message": "Password changed successfully"
                    })
                finally:
                    await db_session.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to change password: {e}")
                raise HTTPException(status_code=500, detail="Failed to change password")

        # ── Feedback API Endpoints ──────────────────────────────────────────
        # Import feedback module (async version for DB support)
        try:
            from .feedback import (
                FeedbackRequest, 
                save_feedback_async, 
                load_feedback_async, 
                mark_feedback_resolved_async,
                migrate_jsonl_to_database
            )
            FEEDBACK_AVAILABLE = True
            
            # Migrate existing JSONL data to database on startup (async)
            # Delay to allow PostgreSQL to fully initialize
            async def _migrate_feedback():
                # Wait for database to be ready
                await asyncio.sleep(5)
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        migrated = await migrate_jsonl_to_database()
                        if migrated > 0:
                            logger.info(f"Migrated {migrated} feedback entries from JSONL to database")
                        return
                    except Exception as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"Feedback migration attempt {attempt + 1} failed: {e}, retrying...")
                            await asyncio.sleep(2)
                        else:
                            logger.warning(f"Failed to migrate feedback after {max_retries} attempts: {e}")
            
            asyncio.create_task(_migrate_feedback())
        except ImportError as e:
            logger.warning(f"Feedback module import failed: {e}")
            FEEDBACK_AVAILABLE = False
            FeedbackRequest = None
            save_feedback_async = None
            load_feedback_async = None
            mark_feedback_resolved_async = None

        @self.app.post("/api/feedback")
        async def submit_feedback(request: Request, _: None = Depends(require_auth)):
            """Submit feedback on an agent response"""
            if not FEEDBACK_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Feedback system is not available"
                )
            
            try:
                body = await request.json()
                feedback_req = FeedbackRequest(**body)
                entry = await save_feedback_async(feedback_req)
                logger.info(f"Feedback submitted: {entry.id} - {entry.category}")
                return JSONResponse({
                    "success": True,
                    "feedback_id": entry.id,
                    "message": "フィードバックを送信しました"
                })
            except Exception as e:
                logger.error(f"Failed to save feedback: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to save feedback: {e}")

        @self.app.get("/api/feedback")
        async def get_feedback_list(
            include_resolved: bool = False,
            limit: int = 100,
            _: None = Depends(require_auth)
        ):
            """Get list of feedback entries (for admin review)"""
            if not FEEDBACK_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Feedback system is not available"
                )
            
            try:
                entries = await load_feedback_async(include_resolved=include_resolved, limit=limit)
                return JSONResponse({
                    "feedback": [entry.model_dump() for entry in entries],
                    "count": len(entries)
                })
            except Exception as e:
                logger.error(f"Failed to load feedback: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to load feedback: {e}")

        @self.app.post("/api/feedback/{feedback_id}/resolve")
        async def resolve_feedback(feedback_id: str, _: None = Depends(require_auth)):
            """Mark a feedback entry as resolved"""
            if not FEEDBACK_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Feedback system is not available"
                )
            
            try:
                success = await mark_feedback_resolved_async(feedback_id)
                if success:
                    return JSONResponse({
                        "success": True,
                        "message": "フィードバックを解決済みにしました"
                    })
                else:
                    raise HTTPException(status_code=404, detail="Feedback not found")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to resolve feedback: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to resolve feedback: {e}")

        # ── RAG API Endpoints ──────────────────────────────────────────
        @self.app.get("/api/rag/status")
        async def get_rag_status(_: None = Depends(require_auth)):
            """Get RAG system status"""
            if not RAG_AVAILABLE:
                return JSONResponse({
                    "available": False,
                    "error": "RAG system is not available"
                })
            
            try:
                manager = get_rag_manager()
                if not await manager.initialize():
                    return JSONResponse({
                        "available": True,
                        "initialized": False,
                        "error": "RAG manager failed to initialize"
                    })
                
                info = await manager.get_collection_info()
                if info:
                    return JSONResponse({
                        "available": True,
                        "initialized": True,
                        "collection_name": info.get("name", "unknown"),
                        "points_count": info.get("points_count", 0),
                        "vectors_count": info.get("vectors_count", 0),
                        "status": info.get("status", "unknown")
                    })
                else:
                    return JSONResponse({
                        "available": True,
                        "initialized": False,
                        "error": "Collection not found"
                    })
            except Exception as e:
                logger.error(f"Failed to get RAG status: {e}")
                return JSONResponse({
                    "available": True,
                    "initialized": False,
                    "error": str(e)
                })

        @self.app.post("/api/rag/index")
        async def create_rag_index(request: RagIndexRequest, _: None = Depends(require_auth)):
            """Create RAG index from specified path"""
            if not RAG_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="RAG system is not available"
                )
            
            target_path = Path(request.path)
            if not target_path.exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"Path not found: {request.path}"
                )
            
            try:
                manager = get_rag_manager()
                if not await manager.initialize():
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to initialize RAG manager"
                    )
                
                # Clear index if requested
                if request.clear:
                    await manager.clear_index()
                    await manager.initialize()  # Re-initialize after clear
                    logger.info("RAG index cleared")
                
                # Index the path
                if target_path.is_file():
                    count = await manager.index_file(str(target_path))
                    result = {
                        "success": True,
                        "type": "file",
                        "path": str(target_path),
                        "chunks_indexed": count,
                        "message": f"ファイル '{target_path.name}' を {count} チャンクでインデックスしました"
                    }
                else:
                    results = await manager.index_directory(str(target_path), recursive=True)
                    total_chunks = sum(results.values())
                    result = {
                        "success": True,
                        "type": "directory",
                        "path": str(target_path),
                        "files_indexed": len(results),
                        "chunks_indexed": total_chunks,
                        "message": f"{len(results)} ファイルを {total_chunks} チャンクでインデックスしました"
                    }
                
                logger.info(f"RAG index created: {result}")
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to create RAG index: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to create RAG index: {e}")

        @self.app.delete("/api/rag/index")
        async def clear_rag_index(_: None = Depends(require_auth)):
            """Clear the RAG index"""
            if not RAG_AVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="RAG system is not available"
                )
            
            try:
                manager = get_rag_manager()
                if not await manager.initialize():
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to initialize RAG manager"
                    )
                
                await manager.clear_index()
                logger.info("RAG index cleared")
                
                return JSONResponse({
                    "success": True,
                    "message": "RAGインデックスをクリアしました"
                })
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to clear RAG index: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to clear RAG index: {e}")

        @self.app.websocket("/ws")

        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket endpoint for real-time communication"""
            if not self._authorize_websocket(websocket):
                await websocket.accept()
                await websocket.close(code=1008)
                return
            await self.manager.connect(websocket)
            
            # Set user context for os_operations permission checks
            await self._setup_user_context(websocket)
            
            try:
                while True:
                    # Receive message from client
                    data = await websocket.receive_json()
                    message_type = data.get("type")
                    
                    if message_type == "user_message":
                        await self._handle_user_message(data.get("data", {}))
                    elif message_type == "clear_chat":
                        await self._handle_clear_chat()
                    elif message_type == "external_llm_permission_response":
                        await self._handle_external_llm_permission_response(data.get("data", {}))
                    elif message_type == "set_llm_mode":
                        await self._handle_set_llm_mode(data.get("data", {}))
                    else:
                        logger.warning(f"Unknown message type: {message_type}")
                        
            except WebSocketDisconnect:
                self.manager.disconnect(websocket)
                # Clear user context on disconnect
                if OS_OPS_CONTEXT_AVAILABLE and clear_user_context:
                    clear_user_context()
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self.manager.disconnect(websocket)
                # Clear user context on error
                if OS_OPS_CONTEXT_AVAILABLE and clear_user_context:
                    clear_user_context()
                
    async def _setup_user_context(self, websocket: WebSocket):
        """
        Set up user context for os_operations permission checks.
        
        Extracts user info from session cookie and sets the context
        so that file operations respect user/project permissions.
        """
        if not OS_OPS_CONTEXT_AVAILABLE or not set_current_user_context:
            return
        
        # Default to admin (backward compat for unauthenticated or fallback)
        user_id = None
        is_admin = True
        project_ids = []
        
        try:
            if self.auth_enabled:
                # Get session from WebSocket cookies
                cookie_header = websocket.headers.get("cookie")
                session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
                
                if session_id:
                    try:
                        serializer = self._get_serializer()
                        if serializer:
                            session_data = serializer.loads(session_id, max_age=self.session_ttl_seconds)
                            username = session_data.get('u')
                            
                            if username and USER_REPOSITORY_AVAILABLE and self._db_manager:
                                # Get user from database
                                db_session = await self._db_manager.get_session()
                                try:
                                    user = await UserRepository.get_by_username(db_session, username)
                                    if user:
                                        user_id = str(user.id)
                                        is_admin = user.role == 'admin'
                                        
                                        # Get user's projects for non-admin users
                                        if not is_admin:
                                            try:
                                                from ..memory.project_repository import ProjectRepository
                                                projects = await ProjectRepository.get_user_projects(db_session, user.id)
                                                project_ids = [str(p.get('id')) for p in projects if p.get('id')]
                                            except Exception as e:
                                                logger.warning(f"Failed to get user projects: {e}")
                                finally:
                                    await db_session.close()
                    except Exception as e:
                        logger.debug(f"Failed to parse session for user context: {e}")
        except Exception as e:
            logger.warning(f"Error setting up user context: {e}")
        
        # Set the context
        set_current_user_context(user_id, is_admin, project_ids)
        logger.debug(f"User context set: user_id={user_id}, is_admin={is_admin}, projects={len(project_ids)}")

    async def _handle_user_message(self, data: dict):
        """Handle user message with optional image, session_id, and project_id"""
        message = data.get("message", "").strip()
        image_data = data.get("image")  # {data: base64, mimeType: str, name: str}
        session_id = data.get("session_id")  # Extract session_id from message data
        project_id = data.get("project_id")  # Extract project_id from message data
        
        if not message and not image_data:
            return
        
        # Set RAG project context for this message
        if RAG_PROJECT_CONTEXT_AVAILABLE and set_rag_project_context:
            set_rag_project_context(project_id)
        
        # Log session ID and project ID for debugging
        log_parts = [f"User message: {message}"]
        if image_data:
            log_parts.append("(with image)")
        if session_id:
            log_parts.append(f"[session_id: {session_id}]")
        if project_id:
            log_parts.append(f"[project_id: {project_id}]")
        if not session_id:
            log_parts.append("[new conversation]")
        logger.info(" ".join(log_parts))
        
        # Create message entry with image info for display
        user_entry = {
            "type": "user",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "has_image": bool(image_data),
            "image_preview": image_data.get("data") if image_data else None
        }
        
        # Broadcast to clients
        self.manager.add_to_history(user_entry)
        await self.manager.broadcast({
            "type": "new_message",
            "data": user_entry
        })
        
        # Call user input callback with session_id and project_id
        if self.on_user_input:
            try:
                if self.main_event_loop:
                    # Run callback in main event loop with session_id and project_id
                    asyncio.run_coroutine_threadsafe(
                        self.on_user_input(message, image_data=image_data, session_id=session_id, project_id=project_id),
                        self.main_event_loop
                    )
                else:
                    # Run in current event loop
                    await self.on_user_input(message, image_data=image_data, session_id=session_id, project_id=project_id)
            except Exception as e:
                logger.error(f"Callback error: {e}")
                await self.add_assistant_message(f"エラーが発生しました: {str(e)}")
                
    async def _handle_clear_chat(self):
        """Handle clear chat request"""
        self.manager.clear_history()
        await self.manager.broadcast({
            "type": "chat_cleared"
        })
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
            self._external_llm_permission_manager = ExternalLLMPermissionManager(self.config)
            
            # Set broadcast callback
            async def broadcast_permission_request(message: dict):
                await self.manager.broadcast(message)
            
            self._external_llm_permission_manager.set_broadcast_callback(broadcast_permission_request)
            
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
        
        self._external_llm_permission_manager.handle_permission_response(request_id, approved)
        logger.info(f"External LLM permission response: {request_id} -> {'approved' if approved else 'denied'}")

    async def _handle_set_llm_mode(self, data: dict):
        """Handle LLM mode change from WebSocket"""
        mode = data.get("mode", "fast")
        
        if mode not in ["fast", "thinking"]:
            logger.warning(f"Invalid LLM mode: {mode}")
            return
        
        # Apply to LLM client if available
        if self._llm_client and hasattr(self._llm_client, 'set_llm_mode'):
            self._llm_client.set_llm_mode(mode)
        
        # Store mode for reference
        self._current_llm_mode = mode
        
        # Broadcast to all clients
        await self.manager.broadcast({
            "type": "llm_mode_change",
            "data": {"mode": mode}
        })
        
        logger.info(f"LLM mode set via WebSocket: {mode}")
        

    async def add_assistant_message(self, message: str):
        """Add assistant message"""
        entry = {
            "type": "assistant",
            "message": message,
            "character": self.character_name,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        
        self.manager.add_to_history(entry)
        await self.manager.broadcast({
            "type": "new_message",
            "data": entry
        })
        logger.info(f"Assistant: {message}")
        
    async def add_system_message(self, message: str):
        """Add system message"""
        entry = {
            "type": "system",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        
        self.manager.add_to_history(entry)
        await self.manager.broadcast({
            "type": "new_message",
            "data": entry
        })
        logger.info(f"System: {message}")
        
    async def add_user_message(self, message: str):
        """Add user message (for voice input)"""
        # Check for duplicate messages
        current_time = time.time()
        if (message == self._last_user_message and 
            current_time - self._last_user_message_time < self._duplicate_threshold):
            logger.info(f"Duplicate user message ignored: {message}")
            return
            
        # Update last message tracking
        self._last_user_message = message
        self._last_user_message_time = current_time
        
        entry = {
            "type": "user",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        
        self.manager.add_to_history(entry)
        await self.manager.broadcast({
            "type": "new_message",
            "data": entry
        })
        logger.info(f"User (voice): {message}")
        
    def set_user_input_callback(self, callback, event_loop=None):
        """Set user input callback"""
        self.on_user_input = callback
        self.main_event_loop = event_loop
    
    def set_clear_chat_callback(self, callback):
        """Set clear chat callback (called when user starts a new conversation)"""
        self.on_clear_chat = callback

    def set_llm_client(self, llm_client):
        """Set LLM client reference for mode management

        Args:
            llm_client: LLM client instance (SGLangClient, AgentLLMClient, etc.)
        """
        self._llm_client = llm_client
        logger.info(f"LLM client set: {type(llm_client).__name__}")

        # HeartbeatRunnerにもLLMクライアントとブロードキャスト関数を注入
        if self._heartbeat_runner:
            self._heartbeat_runner.set_llm_client(llm_client)
            self._heartbeat_runner.set_broadcast_fn(self.manager.broadcast)

    def _extract_mobile_ui_config(self) -> Dict[str, Any]:
        """Safely extract mobile UI configuration"""
        try:
            if hasattr(self.config, 'get_mobile_ui_config'):
                return self.config.get_mobile_ui_config()
            if hasattr(self.config, 'get'):
                return self.config.get('mobile_ui', {})
            if isinstance(self.config, dict):
                return self.config.get('mobile_ui', {})
        except Exception as exc:
            logger.warning(f"モバイルUI設定の取得に失敗しました: {exc}")
        return {}

    def _mobile_commands_enabled(self) -> bool:
        return bool(self.mobile_ui_config.get('enabled', True))

    def _serialize_mobile_commands(self) -> List[Dict[str, Any]]:
        commands: List[Dict[str, Any]] = []
        for cmd in self.mobile_ui_config.get('quick_commands', []):
            if not isinstance(cmd, dict):
                continue
            commands.append({
                'id': cmd.get('id'),
                'label': cmd.get('label', 'コマンド'),
                'hint': cmd.get('hint', ''),
                'icon': cmd.get('icon', 'sparkles'),
                'accent': cmd.get('accent', 'slate'),
                'category': cmd.get('category', 'その他'),
                'action': cmd.get('action', 'send_message'),
                'requires_confirmation': cmd.get('requires_confirmation', False),
                'confirmation_text': cmd.get('confirmation_text', '')
            })
        return commands

    def _get_mobile_command_by_id(self, command_id: str) -> Optional[Dict[str, Any]]:
        for cmd in self.mobile_ui_config.get('quick_commands', []):
            if isinstance(cmd, dict) and cmd.get('id') == command_id:
                return cmd
        return None

    async def _execute_mobile_command(self, command_id: str) -> Dict[str, Any]:
        command = self._get_mobile_command_by_id(command_id)
        if not command:
            raise HTTPException(status_code=404, detail=f"Command not found: {command_id}")

        action = command.get('action', 'send_message')
        label = command.get('label', command_id)
        logger.info(f"Executing mobile command: %s (%s)", label, action)

        if action == 'send_message':
            payload = (command.get('payload') or '').strip()
            if not payload:
                raise HTTPException(status_code=400, detail="Command payload is empty")
            await self._handle_user_message({
                'message': payload,
                'metadata': {
                    'source': 'mobile_command',
                    'command_id': command_id
                }
            })
            result = 'user_message_sent'
        elif action == 'clear_chat':
            await self._handle_clear_chat()
            result = 'chat_cleared'
        elif action == 'system_message':
            payload = (command.get('payload') or '').strip()
            if payload:
                await self.add_system_message(payload)
            result = 'system_message_added'
        elif action == 'run_script':
            # Check if progress streaming is enabled
            stream_progress = command.get('stream_progress', False)
            if stream_progress:
                result = await self._run_script_with_progress(command, command_id)
            else:
                result = await self._run_script_command(command)
        elif action == 'run_system_command':
            result = await self._run_system_command(command)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported command action: {action}")

        return {
            'success': True,
            'result': result,
            'command': {
                'id': command_id,
                'label': label,
                'action': action
            }
        }
    
    async def _run_script_command(self, command: Dict[str, Any]) -> str:
        """Execute a Python script with optional venv support"""
        import asyncio
        
        script_path = command.get('script_path', '').strip()
        if not script_path:
            raise HTTPException(status_code=400, detail="script_path is required for run_script action")
        
        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(status_code=404, detail=f"Script not found: {script_path}")
        
        # Determine Python executable
        python_executable_override = command.get('python_executable', '').strip()
        use_venv = command.get('venv_python', False)
        
        if python_executable_override:
            # Use specified Python executable
            python_exe = python_executable_override
        elif use_venv:
            # Use venv Python from AoiTalk project
            venv_python = Path(__file__).parent.parent.parent / 'venv' / 'Scripts' / 'python.exe'
            if not venv_python.exists():
                logger.warning(f"Venv python not found at {venv_python}, falling back to system python")
                python_exe = 'python'
            else:
                python_exe = str(venv_python)
        else:
            python_exe = 'python'
        
        # Determine working directory
        working_dir = command.get('working_directory', '').strip()
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
                cwd=cwd
            )
            
            # Wait with timeout (5 minutes)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=300.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(status_code=504, detail="Script execution timed out (5 minutes)")
            
            # Log output
            if stdout:
                logger.info(f"Script stdout: {stdout.decode('utf-8', errors='ignore')[:500]}")
            if stderr:
                logger.warning(f"Script stderr: {stderr.decode('utf-8', errors='ignore')[:500]}")
            
            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}"
                )
            
            return 'script_executed'
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script: {e}")
            raise HTTPException(status_code=500, detail=f"Script execution failed: {str(e)}")
    
    async def _run_script_with_progress(self, command: Dict[str, Any], command_id: str) -> str:
        """Execute a script with real-time progress streaming via WebSocket"""
        import asyncio
        import json as json_lib
        
        script_path = command.get('script_path', '').strip()
        if not script_path:
            raise HTTPException(status_code=400, detail="script_path is required for run_script action")
        
        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(status_code=404, detail=f"Script not found: {script_path}")
        
        # Determine Python executable or script type
        use_venv = command.get('venv_python', False)
        python_executable_override = command.get('python_executable', '').strip()
        
        # Check if it's a .bat file
        is_bat = script_path.lower().endswith('.bat')
        
        if is_bat:
            # For .bat files, execute directly
            cmd = [str(script_file)]
        else:
            # For Python scripts
            if python_executable_override:
                # Use specified Python executable
                python_exe = python_executable_override
            elif use_venv:
                venv_python = Path(__file__).parent.parent.parent / 'venv' / 'Scripts' / 'python.exe'
                if not venv_python.exists():
                    logger.warning(f"Venv python not found at {venv_python}, falling back to system python")
                    python_exe = 'python'
                else:
                    python_exe = str(venv_python)
            else:
                python_exe = 'python'
            cmd = [python_exe, str(script_path)]
        
        logger.info(f"Executing script with progress: {' '.join(cmd)}")
        
        try:
            # Set environment variable to enable progress reporting
            env = os.environ.copy()
            env['REPORT_PROGRESS'] = 'true'
            
            # Execute script
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script_file.parent),
                env=env
            )
            
            # Read stdout line by line and broadcast progress
            async def read_and_broadcast():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    
                    line_text = line.decode('utf-8', errors='ignore').strip()
                    logger.debug(f"Script output: {line_text}")
                    
                    # Check for progress messages
                    if line_text.startswith('PROGRESS:'):
                        try:
                            # Parse JSON progress data
                            json_str = line_text[9:].strip()  # Remove "PROGRESS: " prefix
                            progress_data = json_lib.loads(json_str)
                            
                            # Broadcast to all WebSocket clients
                            await self.manager.broadcast({
                                'type': 'command_progress',
                                'command_id': command_id,
                                'data': progress_data
                            })
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
                raise HTTPException(status_code=504, detail="Script execution timed out (30 minutes)")
            
            # Wait for reading to complete
            await read_task
            
            # Check return code
            if process.returncode != 0:
                # Read stderr
                stderr = await process.stderr.read()
                error_msg = stderr.decode('utf-8', errors='ignore')[:500]
                logger.error(f"Script failed: {error_msg}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}"
                )
            
            return 'script_executed_with_progress'
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script with progress: {e}")
            raise HTTPException(status_code=500, detail=f"Script execution failed: {str(e)}")

    
    async def _run_system_command(self, command: Dict[str, Any]) -> str:
        """Execute a system command (Windows-only)"""
        import asyncio
        
        command_line = command.get('command_line', '').strip()
        if not command_line:
            raise HTTPException(status_code=400, detail="command_line is required for run_system_command action")
        
        logger.info(f"Executing system command: {command_line}")
        
        try:
            # Execute command with timeout
            process = await asyncio.create_subprocess_shell(
                command_line,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True
            )
            
            # Wait with timeout (30 seconds for system commands)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(status_code=504, detail="Command execution timed out (30 seconds)")
            
            # Log output
            if stdout:
                logger.info(f"Command stdout: {stdout.decode('utf-8', errors='ignore')[:500]}")
            if stderr:
                logger.warning(f"Command stderr: {stderr.decode('utf-8', errors='ignore')[:500]}")
            
            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Command failed with exit code {process.returncode}"
                )
            
            return 'system_command_executed'
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute system command: {e}")
            raise HTTPException(status_code=500, detail=f"Command execution failed: {str(e)}")

    
    async def _log_login_event(
        self,
        username: str,
        action: str,
        request: Request,
        success: bool = True,
        failure_reason: Optional[str] = None,
        session_duration: Optional[int] = None
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
                    session_duration_seconds=session_duration
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
            logger.error(f"WebChatServer: キャラクター切り替えコールバック登録エラー: {e}")

    def _load_auth_settings(self) -> tuple[bool, Optional[str], Optional[str], Optional[str], int]:
        """Load authentication settings.
        
        DB認証に完全移行: 
        - ユーザー名/パスワードは環境変数ではなくDBから取得
        - シークレットキーのみ環境変数から取得
        """
        auth_config: Dict[str, Any] = {}
        try:
            if hasattr(self.config, 'get'):
                auth_config = self.config.get('web_interface.auth', {}) or {}
            elif isinstance(self.config, dict):
                auth_config = self.config.get('web_interface', {}).get('auth', {}) or {}
        except Exception as exc:
            logger.warning(f"WebUI 認証設定の取得に失敗しました: {exc}")

        # シークレットキーは環境変数から取得（セッション署名用）
        env_secret = os.getenv('AOITALK_WEB_AUTH_SECRET')
        secret = (env_secret or auth_config.get('secret') or '').strip() or None

        ttl_minutes = auth_config.get('session_ttl_minutes', 1440)
        try:
            ttl_minutes = int(ttl_minutes)
        except (TypeError, ValueError):
            ttl_minutes = 1440

        # DB認証が利用可能かチェック
        if USER_REPOSITORY_AVAILABLE and self._db_manager is not None:
            # DB認証モード: シークレット必須
            enabled = True
            if not secret:
                raise ValueError("WebUI 認証が有効ですが、AOITALK_WEB_AUTH_SECRET が設定されていません")
            logger.info("WebUI 認証: DBベース認証が有効です")
            # username/password は None（DBから取得するため）
            return enabled, None, None, secret, max(60, ttl_minutes * 60)
        else:
            # DB利用不可の場合は認証無効
            logger.warning("WebUI 認証: UserRepositoryが利用不可のため、認証は無効です")
            return False, None, None, None, max(60, ttl_minutes * 60)

    async def _verify_credentials_async(self, username: str, password: str) -> Optional[Any]:
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
                    session=session,
                    username=username,
                    password=password
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
        logger.warning("_verify_credentials called in sync context - use _verify_credentials_async")
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

    def _get_cookie_from_header(self, cookie_header: Optional[str], name: str) -> Optional[str]:
        if not cookie_header:
            return None
        parts = cookie_header.split(';')
        for part in parts:
            if '=' not in part:
                continue
            key, value = part.strip().split('=', 1)
            if key == name:
                return value
        return None

    def _is_request_authenticated(self, request: Request) -> bool:
        if not self.auth_enabled:
            return True
        session_id = request.cookies.get(self.cookie_name)
        return self._verify_session(session_id)

    def _enforce_cookie_auth(self, request: Request) -> None:
        if not self.auth_enabled:
            return
        if not self._is_request_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _get_username_from_request(self, request: Request) -> Optional[str]:
        """Extract username from session cookie.
        
        Returns:
            Username string if session is valid, None otherwise
        """
        if not self.auth_enabled:
            return None
        
        session_id = request.cookies.get(self.cookie_name)
        if not session_id:
            return None
        
        serializer = self._get_serializer()
        if not serializer:
            return None
        
        try:
            session_data = serializer.loads(session_id, max_age=self.session_ttl_seconds)
            return session_data.get('u')
        except (BadSignature, SignatureExpired):
            return None

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
                if user and user.role == 'admin':
                    return True
                return False
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to check admin status: {e}")
            return False
    
    async def _get_user_info_from_request(self, request: Request) -> Optional[Dict[str, Any]]:
        """Get full user info from request session.
        
        Returns:
            User info dict with id, username, role, etc. or None if not authenticated
        """
        username = self._get_username_from_request(request)
        if not username:
            return None
        
        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None
        
        try:
            session = await self._db_manager.get_session()
            try:
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
        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)

        router = create_project_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth
        )
        self.app.include_router(router)
        logger.info("Project routes registered")

    def _register_rag_collection_routes(self):
        """Register RAG collection management routes"""
        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)

        router = create_rag_collection_router(
            get_db_manager=lambda: self._db_manager,
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth
        )
        self.app.include_router(router)
        logger.info("RAG collection routes registered")

    def _register_git_routes(self):
        """Register Git API routes"""
        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)
        
        router = create_git_router(
            get_user_from_request=self._get_user_info_from_request,
            require_auth_dependency=require_auth
        )
        self.app.include_router(router)
        logger.info("Git routes registered")
    
    def _register_conversation_routes(self):
        """Register Conversation History API routes"""
        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)
        
        async def generate_title_via_llm(prompt: str) -> Optional[str]:
            """Generate title using main LLM"""
            try:
                # Try to use the assistant's LLM
                if hasattr(self, 'config') and self.config:
                    # Import LLM manager dynamically
                    from ..llm.manager import LLMManager
                    llm_manager = LLMManager(self.config)
                    response = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: llm_manager.generate_simple(prompt)
                    )
                    return response
            except Exception as e:
                logger.warning(f"LLM title generation failed: {e}")
            return None
        
        router = create_conversation_router(
            require_auth=require_auth,
            get_current_user=self._get_user_info_from_request,
            get_llm_for_title_generation=generate_title_via_llm
        )
        self.app.include_router(router)
        logger.info("Conversation routes registered")

    def _register_skill_routes(self):
        """Register Skills API routes"""
        if not SKILL_ROUTES_AVAILABLE:
            logger.warning("Skill routes not available")
            return

        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)

        router = create_skill_router(require_auth=require_auth)
        self.app.include_router(router)
        logger.info("Skill routes registered")

    def _register_heartbeat_routes(self):
        """Register Heartbeat API routes"""
        if not HEARTBEAT_ROUTES_AVAILABLE:
            logger.warning("Heartbeat routes not available")
            return

        def require_auth(request: Request) -> None:
            self._enforce_cookie_auth(request)

        router = create_heartbeat_router(require_auth=require_auth)
        self.app.include_router(router)
        logger.info("Heartbeat routes registered")

    def _set_session_cookie(self, response: JSONResponse, session_id: str, secure: bool) -> None:
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
        cookie_header = websocket.headers.get("cookie")
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        return self._verify_session(session_id)
    
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
            logger.info(f"WebChatServer: キャラクター切り替えを受信 - {self.character_name} -> {character_name}")
            old_character = self.character_name
            self.character_name = character_name
            
            # WebSocketで接続中のクライアントに通知
            if hasattr(self, 'manager') and self.manager:
                try:
                    # 実行中のイベントループを取得
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.manager.broadcast({
                        "type": "character_switch",
                        "data": {
                            "old_character": old_character,
                            "new_character": character_name,
                            "yaml_filename": yaml_filename,
                            "timestamp": datetime.now().strftime("%H:%M:%S")
                        }
                    }))
                except RuntimeError:
                    # イベントループが実行されていない場合
                    logger.warning("WebChatServer: イベントループが実行されていないため、WebSocket通知をスキップします")
            
            logger.info(f"WebChatServer: キャラクター名を更新しました - {character_name}")
            
        except Exception as e:
            logger.error(f"WebChatServer: キャラクター切り替え処理エラー: {e}")
    
    def set_voice_recognition_ready(self, ready: bool):
        """Set voice recognition ready state"""
        self.voice_recognition_ready = ready
        # Broadcast to all clients
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.manager.broadcast({
                    "type": "voice_status_change",
                    "data": {
                        "ready": ready,
                        "rms": self.current_rms,
                        "recording": self.is_recording
                    }
                }))
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
                asyncio.create_task(self.manager.broadcast({
                    "type": "rms_update",
                    "data": {"rms": rms}
                }))
        except RuntimeError:
            # No event loop, skip broadcast
            pass
        
    def set_recording_state(self, recording: bool):
        """Set recording state"""
        self.is_recording = recording
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.manager.broadcast({
                    "type": "voice_status_change",
                    "data": {
                        "ready": self.voice_recognition_ready,
                        "rms": self.current_rms,
                        "recording": recording
                    }
                }))
        except RuntimeError:
            # No event loop, skip broadcast
            pass
        
    def get_app(self):
        """Get FastAPI app instance"""
        # Mount static files
        static_dir = Path(__file__).parent.parent / "web" / "static"
        if static_dir.exists():
            self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        
        return self.app

def create_web_interface(config, character_name: str):
    """Factory function for WebChatServer"""
    return WebChatServer(config, character_name)
