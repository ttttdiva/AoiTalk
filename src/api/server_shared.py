"""WebChatServer 分割用の共有モジュールレベル定義。

元 server.py のファイル冒頭にあった import 群・可用性フラグ・logger をそのまま移設したもの。
server.py 本体と server_parts/ 配下の各 Mixin が `from .server_shared import *` で参照する。
ロジックは一切変更していない（移動のみ）。
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
from ..features import Features
from .connection_manager import ConnectionManager
from .routes.api_token_routes import register_api_token_routes
from .routes.auth_routes import register_auth_routes
from .routes.agent_run_routes import register_agent_run_routes
from .routes.live_voice_routes import register_live_voice_routes
from .routes.voice_session_routes import register_voice_session_routes
from .routes.voice_session_routes import register_voice_session_routes
from .routes.capabilities_routes import register_capabilities_routes
from .routes.chatgpt_web_routes import register_chatgpt_web_routes
from .routes.config_routes import register_config_routes
from .routes.yomi_linter_routes import register_yomi_linter_routes
from .routes.conversation_dispatch_routes import register_conversation_dispatch_routes
from .routes.crawler_routes import register_crawler_routes
if Features.is_enterprise():
    register_remote_server_routes = None
    register_remote_proxy_routes = None
else:
    from .routes.remote_server_routes import register_remote_server_routes
    from .routes.remote_proxy_routes import register_remote_proxy_routes
from .routes.document_storage_routes import register_document_storage_routes
from .routes.feedback_routes import register_feedback_routes
from .routes.file_explorer_routes import register_file_explorer_routes
from .routes.free_team_routes import register_free_team_routes
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
from ..services.llm_model_catalog import build_llm_mode_state, model_supports_vision
from ..services.media_recognition_service import MediaRecognitionService
from ..services.ollama_model_service import OllamaModelManager
from ..runtime_features import runtime_feature_manager
from ..assistant.chat_attachment_utils import (
    build_message_with_attachment_context,
    inject_media_recognition_results,
    sanitize_chat_attachments,
)
from ..llm.multimodal import normalize_image_payloads

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
from ..llm.planning_policy import resolve_planning_policy
from ..llm.tool_policy import (
    build_command_capability_context,
    command_capabilities_for_current_turn_text,
    filter_review_command_capabilities,
    protect_untrusted_command_context,
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

# Import Project Docs candidate review routes.  Keep this boundary separate
# from the generic memory decision router so approval always goes through the
# canonical Project Information Docs writer.
try:
    from .project_docs_candidate_routes import create_project_docs_candidate_router

    PROJECT_DOCS_CANDIDATE_ROUTES_AVAILABLE = True
except ImportError:
    PROJECT_DOCS_CANDIDATE_ROUTES_AVAILABLE = False
    create_project_docs_candidate_router = None

# Import ProjectContextPack status/rebuild routes.  This boundary remains
# separate from the generic Project and Docs candidate routers so projection
# jobs cannot mutate canonical content directly.
try:
    from .project_context_pack_routes import create_project_context_pack_router

    PROJECT_CONTEXT_PACK_ROUTES_AVAILABLE = True
except ImportError:
    PROJECT_CONTEXT_PACK_ROUTES_AVAILABLE = False
    create_project_context_pack_router = None

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

# Import skill recording routes
try:
    from .skill_recording_routes import create_skill_recording_router

    SKILL_RECORDING_ROUTES_AVAILABLE = True
except ImportError:
    SKILL_RECORDING_ROUTES_AVAILABLE = False
    create_skill_recording_router = None

# Import task event routes
try:
    from .task_event_routes import create_task_router

    TASK_ROUTES_AVAILABLE = True
except ImportError:
    TASK_ROUTES_AVAILABLE = False
    create_task_router = None

# Import Webex routes
try:
    from .webex_routes import create_webex_router

    WEBEX_ROUTES_AVAILABLE = True
except ImportError:
    WEBEX_ROUTES_AVAILABLE = False
    create_webex_router = None

# Import mobile sync routes
try:
    from .sync_routes import create_sync_router

    SYNC_ROUTES_AVAILABLE = True
except ImportError:
    SYNC_ROUTES_AVAILABLE = False
    create_sync_router = None

# Import Docs REST routes
try:
    from .docs_routes import create_docs_router

    DOCS_ROUTES_AVAILABLE = True
except ImportError:
    DOCS_ROUTES_AVAILABLE = False
    create_docs_router = None

# Import authenticated per-user X Cookie routes
try:
    from .x_cookie_routes import create_x_cookie_router

    X_COOKIE_ROUTES_AVAILABLE = True
except ImportError:
    X_COOKIE_ROUTES_AVAILABLE = False
    create_x_cookie_router = None

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

# Import persistent Apps routes
try:
    from .apps_routes import create_apps_router

    APPS_ROUTES_AVAILABLE = True
except ImportError:
    APPS_ROUTES_AVAILABLE = False
    create_apps_router = None

# Hydrus compatibility is a desktop/local integration and is deliberately not
# part of the Enterprise HTTP surface.  Keep this boundary at import time so a
# source-tree Enterprise build cannot register the legacy unauthenticated
# compatibility routes before the publisher's file exclusion runs.
if Features.is_enterprise():
    HYDRUS_ROUTES_AVAILABLE = False
    create_hydrus_compat_router = None
    create_hydrus_router = None
else:
    try:
        from ..tools.hydrus_browser import (
            create_hydrus_compat_router,
            create_hydrus_router,
        )

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

# Import Scenario Studio canonical routes and its read-only legacy projection.
# Enterprise keeps the shared sync/model modules, but does not import or
# register the route modules at all.  This makes the product boundary
# fail-closed even if an optional route dependency is broken at import time.
if Features.is_enterprise():
    STORY_ROUTES_AVAILABLE = False
    create_story_router = None
    STORY_ASSIST_ROUTES_AVAILABLE = False
    create_story_assist_router = None
    STORY_LEGACY_COMPAT_AVAILABLE = False
    create_story_legacy_compat_router = None
    TRPG_REFERENCE_ROUTES_AVAILABLE = False
    create_trpg_reference_router = None
    TRPG_PLAY_ROUTES_AVAILABLE = False
    create_trpg_play_router = None
    TRPG_PLAY_WEBSOCKET_ROUTES_AVAILABLE = False
    register_trpg_play_websocket_routes = None
else:
    try:
        from .story_routes import create_story_router

        STORY_ROUTES_AVAILABLE = True
    except ImportError:
        STORY_ROUTES_AVAILABLE = False
        create_story_router = None

    try:
        from .story_assist_routes import create_story_assist_router

        STORY_ASSIST_ROUTES_AVAILABLE = True
    except ImportError:
        STORY_ASSIST_ROUTES_AVAILABLE = False
        create_story_assist_router = None

    try:
        from .story_legacy_compat import create_story_legacy_compat_router

        STORY_LEGACY_COMPAT_AVAILABLE = True
    except ImportError:
        STORY_LEGACY_COMPAT_AVAILABLE = False
        create_story_legacy_compat_router = None

    try:
        from .trpg_reference_routes import create_trpg_reference_router

        TRPG_REFERENCE_ROUTES_AVAILABLE = True
    except ImportError:
        TRPG_REFERENCE_ROUTES_AVAILABLE = False
        create_trpg_reference_router = None

    try:
        from .trpg_play_routes import create_trpg_play_router

        TRPG_PLAY_ROUTES_AVAILABLE = True
    except ImportError:
        TRPG_PLAY_ROUTES_AVAILABLE = False
        create_trpg_play_router = None

    try:
        from .routes.trpg_play_websocket_routes import register_trpg_play_websocket_routes

        TRPG_PLAY_WEBSOCKET_ROUTES_AVAILABLE = True
    except ImportError:
        TRPG_PLAY_WEBSOCKET_ROUTES_AVAILABLE = False
        register_trpg_play_websocket_routes = None

# ComfyUI can target arbitrary local HTTP endpoints and is disabled by the
# Enterprise product boundary.  Do not register its management API in a
# source-tree Enterprise build; publisher exclusions remain defense in depth.
if Features.is_enterprise():
    COMFYUI_ROUTES_AVAILABLE = False
    create_comfyui_router = None
else:
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
