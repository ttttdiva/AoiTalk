"""
SGLang / Local LLM engine with OpenAI-compatible API

Supports SGLang with automatic server management.
When auto_start is enabled, the SGLang server will be started automatically on Linux.
On Windows, the server must be started externally.
"""
import os
import sys
import time
import signal
import logging
import subprocess
import asyncio
import concurrent.futures
import inspect
import threading
import weakref
import aiohttp
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Union, Generator, Iterator, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    # 型注釈（文字列フォワードリファレンス）用。
    from ..tools.registry import ToolRegistry

from openai import OpenAI

from ..config import Config
from .sglang_url import (
    enforce_enterprise_sglang_model,
    resolve_sglang_base_url,
    resolve_sglang_model,
)
from ..memory.history import HistoryManager
from ..services.project_context import (
    format_project_context_for_chat_prompt,
    format_minimal_project_context_for_chat_prompt,
    ProjectContextResolver,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..tools.registry import get_registry
from ..tools.adapters import OpenAIAPIAdapter
from ..services.story_chat_context import run_story_chat_context_sync
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_cancellation import (
    GenerationInterrupted,
    get_current_generation_cancellation,
)
from .agentic_completion import (
    agentic_max_rounds,
    render_messages_for_review,
    run_agentic_completion_loop_sync,
)
from .agent_runtime import (
    OpenAIToolCallRecord,
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
    run_openai_tool_call_loop,
)
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import ensure_load_tool_pack_tool
from .turn_stream_events import (
    SyncStreamEmitter,
    emit_thinking,
    make_sync_stream_emitter,
    thinking_text_from_message,
)
from .context_snapshot import (
    openai_compatible_request_components,
    reconcile_snapshot,
    sanitized_snapshot_series,
    snapshot,
)
from ..services.context_builder import _needs_detailed_project_context
from ..services.outbound_privacy_service import OutboundPrivacyGateway
from .tool_exposure import filtered_registry_for_client
from .tool_policy import (
    project_progress_review_active,
    reset_current_user_input,
    set_current_user_input,
)
from .provider_capabilities import ProviderCapabilities
from .conversation_context import (
    build_prompt_messages,
    normalize_usage,
    persist_usage_sync,
    stable_cache_key,
)
from .openai_compatible_local_profiles import openai_compatible_server_profile
from .multimodal import openai_content_parts
from .unified_turn_runtime import (
    UnifiedToolResult,
    UnifiedTurnLedger,
    activate_unified_turn_ledger,
)

logger = logging.getLogger(__name__)


@dataclass
class _RunLocalTurnState:
    results: list[UnifiedToolResult] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    failure: str = ""
    updated_at: float = field(default_factory=time.monotonic)


class _LifecycleStream(Iterator[str]):
    """Keep provider generation state alive until a returned stream is closed."""

    def __init__(
        self,
        iterator: Iterator[str],
        on_close: Any,
        on_cancel: Any = None,
    ) -> None:
        self._iterator = iterator
        self._on_close = on_close
        self._on_cancel = on_cancel
        self._state_lock = threading.Lock()
        self._next_active = False
        self._close_requested = False
        self._finalizing = False
        self._closed = False

    def __iter__(self) -> "_LifecycleStream":
        return self

    def __next__(self) -> str:
        with self._state_lock:
            if self._closed or self._close_requested:
                raise StopIteration
            if self._next_active:
                raise RuntimeError("Concurrent _LifecycleStream iteration is rejected")
            self._next_active = True
        try:
            value = next(self._iterator)
        except BaseException:
            should_finalize = self._finish_next(close_requested=True)
            if should_finalize:
                try:
                    self._finalize()
                except BaseException:
                    # Cleanup must not replace the iterator's original exception.
                    pass
            raise
        should_finalize = self._finish_next(close_requested=False)
        if should_finalize:
            self._finalize()
        return value

    def close(self) -> None:
        request_cancel = False
        should_finalize = False
        with self._state_lock:
            if self._closed or self._finalizing:
                return
            if not self._close_requested:
                self._close_requested = True
                request_cancel = self._next_active
            if not self._next_active:
                self._finalizing = True
                should_finalize = True

        if request_cancel and callable(self._on_cancel):
            self._on_cancel()
        if should_finalize:
            self._finalize()

    def _finish_next(self, *, close_requested: bool) -> bool:
        with self._state_lock:
            self._next_active = False
            if close_requested:
                self._close_requested = True
            if self._close_requested and not self._closed and not self._finalizing:
                self._finalizing = True
                return True
            return False

    def _finalize(self) -> None:
        close_error: BaseException | None = None
        try:
            close = getattr(self._iterator, "close", None)
            if callable(close):
                close()
        except BaseException as exc:
            close_error = exc
        finally:
            try:
                self._on_close()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
            finally:
                with self._state_lock:
                    self._closed = True
                    self._finalizing = False

        if close_error is not None:
            raise close_error

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class SGLangServerManager:
    """SGLangサーバーのライフサイクル管理

    Linux環境でSGLangサーバーを自動起動・停止する。
    Windows環境ではサーバー起動をスキップし、外部サーバーへの接続を想定する。
    """

    def __init__(self, config: Optional[Config] = None):
        """Initialize SGLang server manager

        Args:
            config: Application configuration
        """
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self._is_running = False

        # Configuration from config or defaults
        sglang_config = {}
        if config:
            sglang_config = config.get('sglang', {}) or {}

        self.model = enforce_enterprise_sglang_model(
            config,
            "sglang",
            resolve_sglang_model(config),
        )
        self.port = int(os.getenv("SGLANG_PORT", sglang_config.get('port', 30000)))
        self.host = os.getenv("SGLANG_HOST", sglang_config.get('host', '127.0.0.1'))
        self._configured_base_url = resolve_sglang_base_url(config)
        self.mem_fraction_static = sglang_config.get('mem_fraction_static', 0.9)
        self.tensor_parallel_size = sglang_config.get('tensor_parallel_size', 1)
        self.max_model_len = sglang_config.get('max_model_len')  # None = auto
        self.dtype = sglang_config.get('dtype', 'auto')
        self.auto_start = sglang_config.get("auto_start", True)
        self.send_thinking_control = bool(
            sglang_config.get("send_thinking_control", False)
            or "qwen3" in str(self.model).lower()
        )
        # 専用routeで外部endpointが明示された場合は、その接続先を捨てて
        # localhostの別modelを自動起動しない。
        if config and config.get("runtime.target_base_url"):
            self.auto_start = False
        if config and config.get("runtime.disable_server_auto_start"):
            self.auto_start = False
        cache_config = sglang_config.get("cache", {}) or {}
        self.cache_enabled = bool(cache_config.get("enabled", True)) if isinstance(cache_config, dict) else True
        self.cache_extra_args = list(cache_config.get("extra_args", []) or []) if isinstance(cache_config, dict) else []

        # Health check settings
        self.startup_timeout = sglang_config.get('startup_timeout', 300)  # 5 minutes
        self.health_check_interval = sglang_config.get('health_check_interval', 5)

        logger.info(f"[SGLangServerManager] 初期化: model={self.model}, port={self.port}, auto_start={self.auto_start}")

    @property
    def base_url(self) -> str:
        """Get the base URL for the SGLang server"""
        return self._configured_base_url

    @property
    def health_url(self) -> str:
        """Get the server root used by health/readiness checks."""
        base_url = self.base_url.rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url

    def is_linux(self) -> bool:
        """Check if running on Linux"""
        return sys.platform.startswith('linux')

    def is_windows(self) -> bool:
        """Check if running on Windows"""
        return sys.platform == 'win32'

    def check_sglang_installed(self) -> bool:
        """Check if SGLang is installed"""
        try:
            import sglang
            logger.info(f"[SGLangServerManager] SGLang version: {sglang.__version__}")
            return True
        except ImportError:
            return False

    async def is_server_ready(self) -> bool:
        """Check health and that the configured model is actually served."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.health_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        return False
                    async with session.get(
                        f"{self.base_url}/models",
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as models_response:
                        if models_response.status != 200:
                            return False
                        payload = await models_response.json(content_type=None)
                        model_ids = {
                            str(item.get("id"))
                            for item in payload.get("data", [])
                            if isinstance(item, dict) and item.get("id")
                        }
                        return self.model in model_ids
        except Exception:
            return False

    async def wait_for_ready(self, timeout: Optional[int] = None) -> bool:
        """Wait for the SGLang server to become ready

        Args:
            timeout: Maximum time to wait in seconds (default: startup_timeout from config)

        Returns:
            True if server is ready, False if timeout
        """
        timeout = timeout or self.startup_timeout
        start_time = time.time()

        logger.info(f"[SGLangServerManager] サーバー起動待機中... (最大{timeout}秒)")

        while time.time() - start_time < timeout:
            if await self.is_server_ready():
                elapsed = time.time() - start_time
                logger.info(f"[SGLangServerManager] サーバー起動完了 ({elapsed:.1f}秒)")
                return True

            await asyncio.sleep(self.health_check_interval)

            # Check if process is still alive
            if self.process and self.process.poll() is not None:
                logger.error(f"[SGLangServerManager] サーバープロセスが予期せず終了しました (code={self.process.returncode})")
                return False

        logger.error(f"[SGLangServerManager] サーバー起動タイムアウト ({timeout}秒)")
        return False

    async def start(self) -> bool:
        """Start the SGLang server

        Returns:
            True if server started successfully or was already running
        """
        # Check if auto_start is disabled
        if not self.auto_start:
            logger.info("[SGLangServerManager] auto_start無効: 外部サーバーを使用")
            # Check if external server is available
            if await self.is_server_ready():
                logger.info("[SGLangServerManager] 外部サーバー接続確認済み")
                self._is_running = True
                return True
            else:
                logger.warning("[SGLangServerManager] 外部サーバーに接続できません")
                return False

        # Check platform
        if self.is_windows():
            logger.warning("[SGLangServerManager] Windows環境ではSGLangサーバーの自動起動は非対応です")
            logger.warning("[SGLangServerManager] 外部でSGLangサーバーを起動してください")
            # Still try to connect to external server
            if await self.is_server_ready():
                logger.info("[SGLangServerManager] 外部サーバー接続確認済み")
                self._is_running = True
                return True
            return False

        # Check if already running
        if await self.is_server_ready():
            logger.info("[SGLangServerManager] サーバーは既に起動中")
            self._is_running = True
            return True

        # Check if SGLang is installed
        if not self.check_sglang_installed():
            logger.error("[SGLangServerManager] SGLangがインストールされていません")
            logger.error("[SGLangServerManager] pip install sglang[all] でインストールしてください")
            return False

        # Build command
        cmd = self._build_start_command()
        logger.info(f"[SGLangServerManager] サーバー起動: {' '.join(cmd)}")

        from src.utils.log_layout import get_log_layout
        from src.utils.log_housekeeping import rotate_log_if_over_size

        layout = get_log_layout()
        layout.ensure_dirs()
        stdout_path = layout.sglang_server_log()
        stderr_path = layout.sglang_server_error_log()
        rotate_log_if_over_size(stdout_path)
        rotate_log_if_over_size(stderr_path)

        # Open log files
        stdout_log = stdout_path.open("a", encoding="utf-8")
        stderr_log = stderr_path.open("a", encoding="utf-8")

        try:
            # Start server process
            self.process = subprocess.Popen(
                cmd,
                stdout=stdout_log,
                stderr=stderr_log,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None,  # Create new process group on Linux
                env={**os.environ, "CUDA_VISIBLE_DEVICES": os.getenv("CUDA_VISIBLE_DEVICES", "0")}
            )

            logger.info(f"[SGLangServerManager] プロセス起動 (PID={self.process.pid})")

            # Wait for server to be ready
            if await self.wait_for_ready():
                self._is_running = True
                return True
            else:
                # Startup failed, cleanup
                await self.stop()
                return False

        except Exception as e:
            logger.error(f"[SGLangServerManager] サーバー起動エラー: {e}")
            return False

    def _build_start_command(self) -> List[str]:
        """Build the command to start the SGLang server"""
        cmd = [
            sys.executable, "-m", "sglang.launch_server",
            "--model-path", self.model,
            "--host", self.host,
            "--port", str(self.port),
            "--mem-fraction-static", str(self.mem_fraction_static),
            "--tp-size", str(self.tensor_parallel_size),
            "--dtype", self.dtype,
        ]

        if self.max_model_len:
            cmd.extend(["--max-model-len", str(self.max_model_len)])

        # Keep Radix Cache enabled by default.  Version-specific flags are
        # opt-in through extra_args so AoiTalk does not guess unsupported CLI
        # options for a particular SGLang release.
        if self.cache_enabled:
            cmd.extend(str(arg) for arg in self.cache_extra_args)

        # Add trust-remote-code for HuggingFace models
        cmd.append("--trust-remote-code")

        served_model_name = os.getenv("SGLANG_SERVED_MODEL_NAME") or (
            self.config.get("sglang.served_model_name") if self.config else None
        )
        if served_model_name or self.model != "default":
            cmd.extend(["--served-model-name", str(served_model_name or self.model)])

        sglang_config = self.config.get("sglang", {}) if self.config else {}
        reasoning_parser = os.getenv("SGLANG_REASONING_PARSER") or sglang_config.get(
            "reasoning_parser"
        )
        tool_call_parser = os.getenv("SGLANG_TOOL_CALL_PARSER") or sglang_config.get(
            "tool_call_parser"
        )
        if reasoning_parser:
            cmd.extend(["--reasoning-parser", str(reasoning_parser)])
        if tool_call_parser:
            cmd.extend(["--tool-call-parser", str(tool_call_parser)])

        return cmd

    async def stop(self):
        """Stop the SGLang server"""
        if not self.process:
            logger.info("[SGLangServerManager] 停止するプロセスがありません")
            return

        logger.info(f"[SGLangServerManager] サーバー停止中 (PID={self.process.pid})")

        try:
            # Send SIGTERM to process group
            if hasattr(os, 'killpg'):
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()

            # Wait for graceful shutdown
            try:
                self.process.wait(timeout=10)
                logger.info("[SGLangServerManager] サーバー正常終了")
            except subprocess.TimeoutExpired:
                logger.warning("[SGLangServerManager] 強制終了を実行")
                if hasattr(os, 'killpg'):
                    try:
                        os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.kill()
                self.process.wait()

        except Exception as e:
            logger.error(f"[SGLangServerManager] 停止エラー: {e}")

        self.process = None
        self._is_running = False

    def is_running(self) -> bool:
        """Check if the server is running"""
        return self._is_running


class SGLangClient:
    """OpenAI-compatible Local LLM client for SGLang with automatic server management"""

    # ResponseHandler may consume the provider's generator in a worker and
    # check the shared interrupt handle between chunks.  Tool-enabled turns
    # remain on the existing non-streaming path below.
    supports_interruptible_steering = True
    _TOOL_TURN_STATE_TTL_SECONDS = 10 * 60.0
    _TOOL_TURN_STATE_MAX_RUNS = 128

    def __init__(
        self,
        base_url: str = "http://localhost:30000/v1",
        model: str = "default",
        api_key: str = "dummy",
        config: Optional[Config] = None,
        server_manager: Optional[SGLangServerManager] = None
    ):
        """Initialize SGLang/Local LLM client

        Args:
            base_url: OpenAI-compatible API endpoint (e.g., http://localhost:30000/v1)
            model: Model name to use
            api_key: API key (usually not required for local servers)
            config: Application configuration
            server_manager: Optional server manager instance (created automatically if not provided)
        """
        self.config = config
        self._privacy_gateway = OutboundPrivacyGateway(config)
        self.model_name = model
        self.api_key = api_key

        # Server manager for automatic startup
        self.server_manager = server_manager or SGLangServerManager(config)
        self.send_thinking_control = bool(
            getattr(self.server_manager, "send_thinking_control", False)
        )

        # Use base_url from server manager if auto_start is enabled
        if self.server_manager.auto_start:
            self.base_url = self.server_manager.base_url
        else:
            self.base_url = base_url

        # Character settings
        if hasattr(config, 'default_character'):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get('default_character', "Assistant")
        else:
            self.character_name = "Assistant"

        # Initialize OpenAI client with custom base_url
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=api_key
        )
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_response_format=False,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=True,
        )

        # Initialize history manager
        self.history_manager = HistoryManager()
        if config and config.get("use_tools", True):
            self._tool_registry = build_runtime_tool_registry_for_client(
                build_runtime_tool_registry,
                config,
                client=self,
            )
            ensure_load_tool_pack_tool(self._tool_registry, self)
        else:
            self._tool_registry = get_registry()

        # Session context
        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_include_project_context: Optional[bool] = None
        self.current_edit_message_id: Optional[str] = None

        # Build system prompt
        self.system_prompt = self._build_system_prompt()

        # Track if server was started by this client
        self._server_started_by_client = False

        # Thinking mode (Qwen3 specific)
        self._thinking_mode = False  # Default: fast mode
        self.server_profile = openai_compatible_server_profile(
            config, base_url=self.base_url, provider="sglang"
        )
        self._last_model_transcript: list[dict[str, Any]] = []
        self._last_usage: dict[str, Any] = {}
        self._last_usage_run_id: str | None = None
        self._last_tool_calls: list[Any] = []
        self._last_tool_calls_run_id: str | None = None
        self._pending_tool_turn_results: dict[str, _RunLocalTurnState] = {}
        self._completed_tool_turn_results: dict[str, _RunLocalTurnState] = {}
        self._pending_tool_turn_results_lock = threading.Lock()
        self._tool_turn_state_lock = threading.Lock()
        self._generation_owner_lock = threading.Lock()
        self._generation_owner_thread_id: int | None = None
        self._active_streams: weakref.WeakSet[_LifecycleStream] = weakref.WeakSet()
        self._steering_callback_run_ids: set[str] = set()
        self._steering_callback_lock = threading.Lock()
        self._last_tool_loop_messages: list[dict[str, Any]] = []
        self._last_context_snapshots: list[dict[str, Any]] = []
        self._current_dynamic_context: list[tuple[str, str]] = []
        self._current_dynamic_context_metadata: dict[str, dict[str, Any]] = {}

        logger.info(f"[SGLangClient] 初期化完了")
        logger.info(f"[SGLangClient] Base URL: {self.base_url}")
        logger.info(f"[SGLangClient] Model: {model}")
        logger.info(f"[SGLangClient] Character: {self.character_name}")
        logger.info(f"[SGLangClient] Thinking mode: {self._thinking_mode}")

    async def ensure_server_running(self) -> bool:
        """Ensure the SGLang server is running

        Returns:
            True if server is ready, False otherwise
        """
        if self.server_manager.is_running():
            return True

        # Try to start the server
        if await self.server_manager.start():
            self._server_started_by_client = True
            return True

        return False

    def _build_system_prompt(self) -> str:
        """Build system prompt based on character configuration"""
        if not self.config:
            return "あなたは親切なAIアシスタントです。"

        # Load character configuration
        character_config = self.config.get_character_config(self.character_name)
        personality = character_config.get('personality', {})
        character_name = character_config.get('name', self.character_name)

        # Build character instructions
        details = personality.get('details', '')
        return f"あなたは{character_name}です。{details}"

    def set_character(self, character_name: str):
        """Set character and update system prompt

        Args:
            character_name: Name of the character
        """
        self.character_name = character_name
        self.system_prompt = self._build_system_prompt()
        logger.info(f"[SGLangClient] キャラクター変更: {character_name}")

    def update_character(self, yaml_filename: str):
        """Update character from YAML file

        Args:
            yaml_filename: YAML filename (without extension)
        """
        if self.config:
            new_config = self.config.get_character_config(yaml_filename)
            if new_config:
                self.character_name = new_config.get('name', yaml_filename)
                self.clear_history()
                self.system_prompt = self._build_system_prompt()
                logger.info(f"[SGLangClient] キャラクター更新: {self.character_name}")
            else:
                logger.warning(f"[SGLangClient] キャラクター設定が見つかりません: {yaml_filename}")

    def set_system_prompt(self, prompt: str):
        """Set custom system prompt

        Args:
            prompt: System prompt
        """
        self.system_prompt = prompt

    def set_thinking_mode(self, enabled: bool):
        """Set thinking mode for Qwen3 models

        Args:
            enabled: True for thinking mode (deeper reasoning), False for fast mode
        """
        self._thinking_mode = enabled
        logger.info(f"[SGLangClient] Thinking mode set to: {'enabled' if enabled else 'disabled'}")

    def get_thinking_mode(self) -> bool:
        """Get current thinking mode status

        Returns:
            True if thinking mode is enabled
        """
        return self._thinking_mode

    def _process_thinking_response(self, response_text: str) -> tuple:
        """Process response containing thinking tags (Qwen3 specific)

        Args:
            response_text: Raw response text that may contain <think>...</think> tags

        Returns:
            tuple: (visible_response, thinking_content)
        """
        import re

        # Extract thinking content
        think_pattern = r'<think>(.*?)</think>'
        think_matches = re.findall(think_pattern, response_text, re.DOTALL)
        thinking_content = '\n'.join(think_matches)

        # Remove thinking tags from visible response
        visible_response = re.sub(think_pattern, '', response_text, flags=re.DOTALL).strip()

        if thinking_content:
            logger.info(f"[SGLangClient] Thinking content extracted: {len(thinking_content)} chars")

        return visible_response, thinking_content

    def set_llm_mode(self, mode: str):
        """Set LLM response mode (common interface)

        Args:
            mode: 'fast' for quick responses, 'thinking' for deeper reasoning
        """
        if mode not in ['fast', 'thinking']:
            logger.warning(f"[SGLangClient] Invalid mode '{mode}', defaulting to 'fast'")
            mode = 'fast'

        self.set_thinking_mode(mode == 'thinking')

    def get_llm_mode(self) -> str:
        """Get current LLM response mode (common interface)

        Returns:
            Current mode ('fast' or 'thinking')
        """
        return 'thinking' if self._thinking_mode else 'fast'


    def set_session_context(self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        """Update session identifiers"""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata = {
            "model_transcript": [dict(item) for item in self._last_model_transcript],
            "cache_usage": dict(self._last_usage),
            "cache_diagnostics": {
                "provider": "sglang",
                "model": self.model_name,
                "cache_provider": "sglang",
                "cache_mode": "radix",
                "cache_supported": True,
                "cache_active": self._last_usage.get("cache_active"),
                "cache_configured": True,
                "metrics_source": "sglang_response_or_server_metrics",
                "cache_key": getattr(self, "_cache_key", None),
                "session_affinity": self.server_profile.get("supports_session_affinity"),
            },
        }
        if self._last_context_snapshots:
            bounded = sanitized_snapshot_series(self._last_context_snapshots)
            if bounded:
                metadata["context_snapshot"] = bounded
        return metadata

    def _create_observed_completion(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_kind: str = "chat.completions",
        **transport_kwargs: Any,
    ) -> Any:
        gateway = self._sync_privacy_gateway()
        api_kwargs = gateway.protect_sync(
            api_kwargs,
            provider="sglang",
            base_url=self.base_url,
            source_kind="model_request",
        ).payload
        try:
            values = list(getattr(self, "_last_context_snapshots", []) or [])
            values.append(
                snapshot(
                    provider="sglang",
                    model=str(api_kwargs.get("model") or self.model_name),
                    components=openai_compatible_request_components(
                        api_kwargs.get("messages") or [],
                        api_kwargs.get("tools") or [],
                        provider="sglang",
                        dynamic_context=getattr(
                        self, "_current_dynamic_context", []
                    ),
                    dynamic_context_metadata=getattr(
                        self, "_current_dynamic_context_metadata", {}
                    ),
                    ),
                    request_index=len(values),
                    request_kind=request_kind,
                )
            )
            self._last_context_snapshots = values[-32:]
        except Exception:
            logger.warning(
                "[SGLangClient] context observation failed; continuing",
                exc_info=True,
            )
        return self.client.chat.completions.create(
            **api_kwargs,
            **transport_kwargs,
        )

    def _capture_usage(self, response: Any) -> dict[str, Any]:
        raw = getattr(response, "usage", None)
        resolved_model = getattr(response, "model", None)
        if raw is None and isinstance(response, dict):
            raw = response.get("usage")
        if resolved_model is None and isinstance(response, dict):
            resolved_model = response.get("model")
        captured_usage = {
            key: value
            for key, value in normalize_usage(
                raw,
                provider="sglang",
                resolved_model=(str(resolved_model) if resolved_model else None),
            ).items()
            if value is not None
        }
        self._last_usage = captured_usage
        input_tokens = self._last_usage.get("input_tokens")
        context_snapshots = getattr(self, "_last_context_snapshots", None)
        if input_tokens is not None and context_snapshots:
            context_snapshots[-1] = reconcile_snapshot(
                context_snapshots[-1],
                int(input_tokens),
            )
        return captured_usage

    def _capture_and_persist_usage(
        self,
        response: Any,
        *,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> dict[str, Any]:
        """Capture and persist one direct SGLang response."""

        previous_usage = dict(getattr(self, "_last_usage", {}) or {})
        usage = self._capture_usage(response)
        if not usage and previous_usage:
            self._last_usage = previous_usage
        if usage:
            persist_usage_sync(
                self,
                provider="sglang",
                model=self.model_name,
                usage=usage,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )
        return usage

    def _capture_turn_usage(
        self,
        response: Any,
        ledger: UnifiedTurnLedger,
    ) -> dict[str, Any]:
        captured_usage = self._capture_usage(response)
        ledger.record_usage(captured_usage)
        self._last_usage_run_id = str(ledger.run_id or "").strip() or None
        return captured_usage

    def _create_turn_completion(
        self,
        api_kwargs: Dict[str, Any],
        ledger: UnifiedTurnLedger,
        *,
        request_type: str = "chat",
        **transport_kwargs: Any,
    ) -> Any:
        started_at = time.monotonic()
        response = self._create_observed_completion(
            api_kwargs,
            **transport_kwargs,
        )
        usage = self._capture_turn_usage(response, ledger)
        persist_usage_sync(
            self,
            provider="sglang",
            model=self.model_name,
            usage=usage,
            request_type=request_type,
            latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        )
        return response

    def _record_model_transcript(self, messages: List[Dict[str, Any]], response_text: str) -> None:
        source_messages = self._last_tool_loop_messages or messages
        self._last_model_transcript = [
            dict(message)
            for message in source_messages
            if message.get("role") in {"user", "assistant", "tool"}
        ]
        if response_text:
            self._last_model_transcript.append({"role": "assistant", "content": response_text})
        self.history_manager.set_model_messages(self._last_model_transcript)

    def _build_messages(
        self,
        user_input: str,
        *,
        dynamic_context: Optional[list[tuple[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        """Build messages list for API call

        Args:
            user_input: Current user input

        Returns:
            List of message dicts for OpenAI API
        """
        history = self.history_manager.get_model_messages()
        context_window = self.history_manager.context_window_size
        return [
            {"role": "system", "content": self.system_prompt},
            *build_prompt_messages(
                history[-(context_window * 2):],
                summary=self.history_manager.summary,
                current_user_input=user_input,
                dynamic_context=dynamic_context or [],
            ),
        ]

    def _run_async_sync(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _get_story_chat_context_sync(self):
        """Resolve the trusted StoryWritingSession for this conversation."""
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(
            self._run_async_sync,
            str(self.current_session_id),
        )

    def _consume_steering_callback(
        self,
        steering_callback: Any,
        run_id: str,
    ) -> list[str]:
        """Consume late steering once for a logical generation run."""

        if not callable(steering_callback):
            return []
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            with self._steering_callback_lock:
                if normalized_run_id in self._steering_callback_run_ids:
                    return []

        value = steering_callback()
        if inspect.isawaitable(value):
            value = self._run_async_sync(value)
        if isinstance(value, dict):
            value = value.get("instructions", value.get("instruction", []))
        if isinstance(value, str):
            values = [value]
        else:
            try:
                values = list(value or [])
            except TypeError:
                values = [value]
        instructions = [
            str(item).strip() for item in values if str(item).strip()
        ]
        if normalized_run_id:
            with self._steering_callback_lock:
                self._steering_callback_run_ids.add(normalized_run_id)
        return instructions

    @staticmethod
    def _append_steering_instructions(
        user_input: str,
        instructions: list[str],
    ) -> str:
        if not instructions:
            return user_input
        block = "追加指示:\n" + "\n".join(
            f"- {instruction}" for instruction in instructions
        )
        return f"{user_input}\n\n{block}" if user_input else block

    def _get_session_user_id(self) -> str:
        return getattr(self, "session_user_id", None) or "default_user"

    def _sync_privacy_gateway(self) -> OutboundPrivacyGateway:
        """Reuse or recreate the privacy gateway for the active session.

        Memory/title extraction runs on the same long-lived provider client as
        normal turns.  Keep its alias scope and effective session/project
        policy aligned when the client is reused or when a session switch
        requires a fresh gateway.
        """

        user_id = str(self._get_session_user_id() or "default_user")
        session_id = str(getattr(self, "current_session_id", None) or "")
        gateway = getattr(self, "_privacy_gateway", None)
        if (
            gateway is None
            or gateway.user_id != user_id
            or gateway.session_id != session_id
        ):
            self._privacy_gateway = OutboundPrivacyGateway(
                getattr(self, "config", None),
                user_id=user_id,
                session_id=session_id,
                session_context=getattr(self, "_privacy_session_context", None),
                project_metadata=getattr(self, "_privacy_project_metadata", None),
            )
        else:
            session_context = getattr(self, "_privacy_session_context", None)
            project_metadata = getattr(self, "_privacy_project_metadata", None)
            if session_context or project_metadata:
                self._privacy_gateway.update_policy_context(
                    session_context=session_context,
                    project_metadata=project_metadata,
                )
            else:
                self._privacy_gateway.update_policy_context()
        return self._privacy_gateway

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        current_project_id = getattr(self, "current_project_id", None)
        current_session_id = getattr(self, "current_session_id", None)
        if not current_project_id and not current_session_id:
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_sync(
                resolver.resolve_context(
                    project_id=current_project_id,
                    session_id=current_session_id,
                    user_id=self._get_session_user_id(),
                )
            )
        except Exception as exc:
            logger.warning("[SGLangClient] Failed to resolve project context: %s", exc)
            return None

    def _build_tool_hint_context(self, user_input: str) -> str:
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=filtered_registry_for_client(self, self._tool_registry),
            policy=get_client_generation_policy(self),
            log_prefix="SGLangClient",
        )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        self._last_context_snapshots = []
        self._current_dynamic_context = []
        self._current_dynamic_context_metadata = {}
        response = self._create_observed_completion(
            {
                "model": self.model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens or 1024,
            }
        )
        self._capture_and_persist_usage(response, request_type="chat")
        return self._privacy_gateway.restore(
            response.choices[0].message.content or ""
        )

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
        request_type: str = "memory_extraction",
    ) -> str:
        """Run side-effect-free extraction without touching turn/history state."""
        gateway = self._sync_privacy_gateway()

        def create_extraction() -> Any:
            started_at = time.monotonic()
            extraction_kwargs = gateway.protect_sync(
                {
                    "model": self.model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 1200,
                },
                provider="sglang",
                base_url=self.base_url,
                source_kind=request_type,
            ).payload
            response = self.client.chat.completions.create(
                **extraction_kwargs,
            )
            self._capture_and_persist_usage(
                response,
                request_type=request_type,
                latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            )
            return response

        response = await asyncio.to_thread(create_extraction)
        return gateway.restore(
            str(response.choices[0].message.content or "")
        )

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """履歴・ツールを変更せずプレーンテキストを生成する。"""
        return await self.generate_memory_extraction_async(
            prompt,
            system_prompt=(
                system_prompt
                or "You are a concise assistant. Do not call tools or modify files."
            ),
            request_type="plain",
        )

    async def generate_title_async(self, prompt: str) -> str:
        """Generate a side-effect-free SGLang title and meter it separately."""

        return await self.generate_memory_extraction_async(
            prompt,
            system_prompt="You generate concise titles. Return only the title.",
            request_type="title",
        )

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        self._last_context_snapshots = []
        self._current_dynamic_context = []
        self._current_dynamic_context_metadata = {}
        stream = self._create_observed_completion(
            {
                "model": self.model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens or 1024,
                "stream": True,
            },
            request_kind="chat.completions.stream",
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield self._privacy_gateway.restore_aliases(
                    chunk.choices[0].delta.content
                )

    def list_models(self) -> List[Dict[str, Any]]:
        response = self.client.models.list()
        data = getattr(response, "data", response)
        models = []
        for item in data:
            model_id = getattr(item, "id", None)
            if model_id is None and isinstance(item, dict):
                model_id = item.get("id")
            if model_id:
                models.append({"id": model_id})
        return models

    def health_check(self) -> Dict[str, Any]:
        try:
            models = self.list_models()
            return {"ok": True, "base_url": self.base_url, "model_count": len(models)}
        except Exception as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc)}

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
        steering_callback: Any = None,
    ) -> Union[str, Generator[str, None, None]]:
        """Generate response using SGLang/Local LLM

        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            stream: Whether to stream response
            image_data: Optional image data (not supported for SGLang text models, will be ignored)
            stream_callback: Optional stream event callback (assistant_text / thinking)
            steering_callback: Optional late steering callback supplied by ResponseHandler

        Returns:
            Generated response text or generator
        """
        self._acquire_generation_state()
        stream_lifecycle_transferred = False
        cancellation_handle = get_current_generation_cancellation()
        run_id = (
            str(cancellation_handle.run_id or "").strip()
            if cancellation_handle is not None
            else ""
        )
        try:
            steering_instructions = self._consume_steering_callback(
                steering_callback,
                run_id,
            )
        except BaseException:
            self._release_generation_state()
            raise
        user_input = self._append_steering_instructions(
            user_input,
            steering_instructions,
        )
        turn_ledger = self._begin_tool_turn_attempt(run_id)
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )
        try:
            self._last_tool_loop_messages = []
            self._last_context_snapshots = []
            self._current_dynamic_context_metadata = {}
            project_context_started = time.perf_counter()
            project_context = self._resolve_project_context_sync()
            project_context_duration_ms = (
                time.perf_counter() - project_context_started
            ) * 1000
            project_token = set_runtime_project_context(project_context)
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            if self._privacy_session_context or self._privacy_project_metadata:
                self._privacy_gateway.update_policy_context(
                    session_context=self._privacy_session_context,
                    project_metadata=self._privacy_project_metadata,
                )
            else:
                self._privacy_gateway.update_policy_context()
            tool_hint_started = time.perf_counter()
            tool_hint_context = self._build_tool_hint_context(
                user_input
            )
            tool_hint_duration_ms = (
                time.perf_counter() - tool_hint_started
            ) * 1000
            dynamic_context: list[tuple[str, str]] = []
            model_project_context = (
                project_context if project_context_enabled_for_client(self) else None
            )
            if model_project_context:
                dynamic_context.append(
                    (
                        "Current Project Context",
                        (
                            format_project_context_for_chat_prompt(model_project_context)
                            if _needs_detailed_project_context(user_input)
                            else format_minimal_project_context_for_chat_prompt(
                                model_project_context
                            )
                        ),
                    )
                )
            if tool_hint_context:
                dynamic_context.append(("Current tool hints", tool_hint_context))
            self._current_dynamic_context = list(dynamic_context)
            self._current_dynamic_context_metadata = {
                "Current Project Context": {
                    "duration_ms": project_context_duration_ms
                },
                "Current tool hints": {
                    "duration_ms": tool_hint_duration_ms
                },
            }

            # Build messages
            messages = self._build_messages(
                user_input,
                dynamic_context=dynamic_context,
            )
            if image_data and messages:
                messages[-1]["content"] = openai_content_parts(
                    str(messages[-1].get("content") or ""),
                    image_data,
                )

            # Mode-specific parameters (Qwen3 thinking mode support)
            if not getattr(self, "send_thinking_control", False):
                effective_temperature = temperature
                effective_top_p = 0.8
                extra_body = {}
            elif self._thinking_mode:
                # Thinking mode: deeper reasoning with enable_thinking=True
                effective_temperature = 0.6
                effective_top_p = 0.95
                extra_body = {"chat_template_kwargs": {"enable_thinking": True}}
                logger.info("[SGLangClient] Using thinking mode (enable_thinking=True)")
            else:
                # Fast mode: quick responses with enable_thinking=False
                effective_temperature = temperature
                effective_top_p = 0.8
                extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

            # Build tools from unified registry
            registry = filtered_registry_for_client(self, self._tool_registry)
            api_tools = OpenAIAPIAdapter.convert_all(registry.get_all()) if len(registry) > 0 else None
            self._cache_key = stable_cache_key(
                user_id=self.session_user_id,
                session_id=self.current_session_id,
                project_id=self.current_project_id,
                character=self.character_name,
                model=self.model_name,
                system_prompt=self.system_prompt,
                tool_schemas=api_tools or [],
                provider="sglang",
                branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
                summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
                server_instance=str(self.session_metadata.get("server_instance") or "default-instance"),
            )

            # Make API call
            if (
                stream
                and not api_tools
                and not project_progress_review_active(user_input)
            ):
                stream = self._stream_response(
                    messages,
                    effective_temperature,
                    max_tokens,
                    user_input,
                    effective_top_p,
                    extra_body,
                    turn_ledger=turn_ledger,
                )
                # Context is no longer needed after messages are built.  Reset it
                # now because the stream may be consumed in another thread.
                if project_token is not None:
                    reset_runtime_project_context(project_token)
                    project_token = None
                reset_current_generation_policy(generation_policy_token)
                generation_policy_token = None
                reset_current_user_input(tool_policy_token)
                tool_policy_token = None
                stream_lifecycle_transferred = True
                lifecycle_stream = _LifecycleStream(
                    stream,
                    lambda: self._finalize_generation_stream(run_id),
                    (
                        cancellation_handle.cancel_requested.set
                        if cancellation_handle is not None
                        else None
                    ),
                )
                self._active_streams.add(lifecycle_stream)
                return lifecycle_stream
            else:
                # Build common kwargs
                api_kwargs: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": effective_temperature,
                    "top_p": effective_top_p,
                    "max_tokens": max_tokens or 1024,
                }
                if api_tools:
                    api_kwargs["tools"] = api_tools
                    api_kwargs["tool_choice"] = "auto"
                self._cache_key = stable_cache_key(
                    user_id=self.session_user_id,
                    session_id=self.current_session_id,
                    project_id=self.current_project_id,
                    character=self.character_name,
                    model=self.model_name,
                    system_prompt=self.system_prompt,
                    tool_schemas=api_tools or [],
                    provider="sglang",
                    branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
                    summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
                    server_instance=str(self.session_metadata.get("server_instance") or "default-instance"),
                )

                # Try with extra_body first, fallback without if model doesn't support it
                try:
                    response = self._create_turn_completion(
                        api_kwargs,
                        turn_ledger,
                        extra_body=extra_body,
                    )
                except Exception as api_err:
                    if "chat_template" in str(api_err).lower() or "extra_body" in str(api_err).lower():
                        logger.warning(f"[SGLangClient] Retrying without extra_body: {api_err}")
                        response = self._create_turn_completion(
                            api_kwargs,
                            turn_ledger,
                            request_kind="chat.completions.retry",
                            request_type="retry",
                        )
                    else:
                        raise

                choice = response.choices[0]
                turn_event_emitter = make_sync_stream_emitter(stream_callback)

                # Handle tool calls if present
                if choice.message.tool_calls:
                    response_text = self._handle_tool_calls(
                        messages,
                        choice.message,
                        api_kwargs,
                        registry,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        turn_ledger=turn_ledger,
                    )
                elif project_progress_review_active(user_input):
                    response_text = self._handle_tool_calls(
                        messages,
                        choice.message,
                        api_kwargs,
                        registry,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        turn_ledger=turn_ledger,
                    )
                else:
                    raw_response_text = choice.message.content or ""
                    if self._thinking_mode:
                        response_text, thinking_content = self._process_thinking_response(
                            raw_response_text
                        )
                    else:
                        response_text = raw_response_text
                        thinking_content = ""
                    # reasoning フィールド優先、無ければ <think> 抽出結果を配信する。
                    emit_thinking(
                        turn_event_emitter,
                        thinking_text_from_message(choice.message) or thinking_content,
                        round_index=0,
                    )
                    response_text = guard_tool_execution_claims(response_text, [])

                response_text = run_agentic_completion_loop_sync(
                    client=self,
                    run_once=lambda prompt: self._run_agentic_review_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        user_input=user_input,
                        turn_ledger=turn_ledger,
                    ),
                    context=render_messages_for_review(messages),
                    user_input=user_input,
                    initial_response=response_text,
                )
                response_text = self._privacy_gateway.restore(response_text)
                self._record_model_transcript(messages, response_text)
                # Add to history
                self.history_manager.add_message("user", user_input)
                self.history_manager.add_message("assistant", response_text)

                self._complete_tool_turn_attempt(turn_ledger)
                logger.info(f"[SGLangClient] 応答生成完了: {len(response_text)}文字")
                return response_text

        except GenerationInterrupted:
            self.seed_next_tool_turn_attempt(turn_ledger)
            raise
        except Exception as e:
            turn_ledger.failure = f"{type(e).__name__}: {e}"
            self._complete_tool_turn_attempt(turn_ledger)
            logger.error(f"[SGLangClient] エラー: {e}")
            import traceback
            traceback.print_exc()

            fallback = self._get_fallback_response()
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", fallback)

            if stream:
                def error_generator():
                    yield fallback
                return error_generator()
            return fallback
        finally:
            if project_token is not None:
                reset_runtime_project_context(project_token)
            if generation_policy_token is not None:
                reset_current_generation_policy(generation_policy_token)
            if tool_policy_token is not None:
                reset_current_user_input(tool_policy_token)
            if not stream_lifecycle_transferred:
                self._release_generation_state()

    def _acquire_generation_state(self) -> None:
        """Serialize client state and reject same-thread nested generation."""

        thread_id = threading.get_ident()
        with self._generation_owner_lock:
            if self._generation_owner_thread_id == thread_id:
                raise RuntimeError(
                    "Nested SGLang generate_response on the same client is rejected"
                )
        self._tool_turn_state_lock.acquire()
        with self._generation_owner_lock:
            self._generation_owner_thread_id = thread_id

    def _release_generation_state(self) -> None:
        with self._generation_owner_lock:
            self._generation_owner_thread_id = None
        self._tool_turn_state_lock.release()

    def _finalize_generation_stream(self, run_id: str) -> None:
        """Release stream-owned state without deleting an interrupt retry seed."""

        normalized_run_id = str(run_id or "").strip()
        has_run_state = False
        if normalized_run_id:
            with self._pending_tool_turn_results_lock:
                self._prune_tool_turn_states_locked()
                has_run_state = (
                    normalized_run_id in self._pending_tool_turn_results
                    or normalized_run_id in self._completed_tool_turn_results
                )
            if not has_run_state:
                self.discard_generation_run(normalized_run_id)
        else:
            with self._pending_tool_turn_results_lock:
                self._last_tool_calls = []
                self._last_tool_calls_run_id = None
                self._last_usage = {}
                self._last_usage_run_id = None
        self._release_generation_state()

    @staticmethod
    def _tool_call_record(result: UnifiedToolResult) -> OpenAIToolCallRecord:
        return OpenAIToolCallRecord(
            tool=result.call.tool,
            arguments=dict(result.call.arguments),
            result=result.model_output,
        )

    def _record_tool_result(self, result: UnifiedToolResult) -> None:
        self._last_tool_calls.append(self._tool_call_record(result))

    def seed_next_tool_turn_attempt(
        self,
        ledger: UnifiedTurnLedger,
    ) -> None:
        """Seed only the next steering retry with this logical turn's results."""

        run_id = str(ledger.run_id or "").strip()
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            if run_id and (ledger.results or ledger.usage or ledger.failure):
                self._pending_tool_turn_results[run_id] = _RunLocalTurnState(
                    results=list(ledger.results),
                    usage=dict(ledger.usage),
                    failure=ledger.failure,
                )
                self._completed_tool_turn_results.pop(run_id, None)
                self._prune_tool_turn_states_locked()
            elif run_id:
                self._pending_tool_turn_results.pop(run_id, None)

    def prepare_generation_retry(self, run_id: str | None) -> bool:
        """Move an accepted-at-provider result back to the same run's retry seed."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return False
        current_handle = get_current_generation_cancellation()
        if (
            current_handle is None
            or str(current_handle.run_id or "").strip() != normalized_run_id
        ):
            return False
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            completed = self._completed_tool_turn_results.pop(
                normalized_run_id,
                None,
            )
            if completed is None or not (
                completed.results or completed.usage or completed.failure
            ):
                return False
            completed.updated_at = time.monotonic()
            self._pending_tool_turn_results[normalized_run_id] = completed
            return True

    def accept_generation_run(self, run_id: str | None) -> None:
        """Discard retry-only state once ResponseHandler accepts the response."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            self._pending_tool_turn_results.pop(normalized_run_id, None)
        with self._steering_callback_lock:
            self._steering_callback_run_ids.discard(normalized_run_id)

    def discard_generation_run(self, run_id: str | None) -> None:
        """Remove all secret-bearing state for a failed or cancelled run."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            with self._pending_tool_turn_results_lock:
                self._last_tool_calls = []
                self._last_tool_calls_run_id = None
                self._last_usage = {}
                self._last_usage_run_id = None
            return
        with self._pending_tool_turn_results_lock:
            self._pending_tool_turn_results.pop(normalized_run_id, None)
            self._completed_tool_turn_results.pop(normalized_run_id, None)
            self._clear_legacy_run_state_locked(normalized_run_id)
        with self._steering_callback_lock:
            self._steering_callback_run_ids.discard(normalized_run_id)

    def peek_completed_agent_run_state(
        self,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Read run-local audit state without removing retryable evidence."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return {"tool_calls": [], "usage": {}}
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            state = self._completed_tool_turn_results.get(normalized_run_id)
            if state is None:
                return {"tool_calls": [], "usage": {}}
            payload = {
                "tool_calls": [
                    self._tool_call_record(result) for result in state.results
                ],
                "usage": dict(state.usage),
            }
            if state.failure:
                payload["failure"] = state.failure
            return payload

    def ack_completed_agent_run_state(self, run_id: str | None) -> bool:
        """Remove a run's evidence only after durable AgentRun terminalization."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return False
        with self._pending_tool_turn_results_lock:
            removed = self._completed_tool_turn_results.pop(
                normalized_run_id,
                None,
            )
            self._clear_legacy_run_state_locked(normalized_run_id)
        return removed is not None

    def consume_completed_agent_run_state(
        self,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Atomically consume only the completed state belonging to ``run_id``."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return {"tool_calls": [], "usage": {}}
        state = self.peek_completed_agent_run_state(normalized_run_id)
        self.ack_completed_agent_run_state(normalized_run_id)
        return state

    def _clear_legacy_run_state_locked(self, run_id: str) -> None:
        if self._last_tool_calls_run_id == run_id:
            self._last_tool_calls = []
            self._last_tool_calls_run_id = None
        if self._last_usage_run_id == run_id:
            self._last_usage = {}
            self._last_usage_run_id = None

    def _complete_tool_turn_attempt(
        self,
        ledger: UnifiedTurnLedger,
        *,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        run_id = str(ledger.run_id or "").strip()
        if not run_id:
            return
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            self._pending_tool_turn_results.pop(run_id, None)
            self._completed_tool_turn_results[run_id] = _RunLocalTurnState(
                results=list(ledger.results),
                usage=dict(ledger.usage or usage or {}),
                failure=ledger.failure,
            )
            self._prune_tool_turn_states_locked()

    def _prune_tool_turn_states_locked(self) -> None:
        now = time.monotonic()
        ttl = max(0.0, float(self._TOOL_TURN_STATE_TTL_SECONDS))
        pruned_run_ids: set[str] = set()
        maps = (
            self._pending_tool_turn_results,
            self._completed_tool_turn_results,
        )
        for state_map in maps:
            expired = [
                run_id
                for run_id, state in state_map.items()
                if now - state.updated_at >= ttl
            ]
            for run_id in expired:
                if state_map.pop(run_id, None) is not None:
                    pruned_run_ids.add(run_id)

        max_runs = max(1, int(self._TOOL_TURN_STATE_MAX_RUNS))
        states = sorted(
            (
                (state.updated_at, kind, run_id)
                for kind, state_map in enumerate(maps)
                for run_id, state in state_map.items()
            ),
            key=lambda item: item[0],
        )
        for _, kind, run_id in states[:-max_runs]:
            if maps[kind].pop(run_id, None) is not None:
                pruned_run_ids.add(run_id)

        for run_id in pruned_run_ids:
            if all(run_id not in state_map for state_map in maps):
                self._clear_legacy_run_state_locked(run_id)

    def _begin_tool_turn_attempt(self, run_id: str | None) -> UnifiedTurnLedger:
        normalized_run_id = str(run_id or "").strip()
        with self._pending_tool_turn_results_lock:
            self._prune_tool_turn_states_locked()
            pending = (
                self._pending_tool_turn_results.pop(normalized_run_id, None)
                if normalized_run_id
                else None
            )
            if normalized_run_id:
                self._completed_tool_turn_results.pop(normalized_run_id, None)
            seed_results = list(pending.results) if pending is not None else []
            seed_usage = dict(pending.usage) if pending is not None else {}
            seed_failure = pending.failure if pending is not None else ""
        self._last_tool_calls = [
            self._tool_call_record(result) for result in seed_results
        ]
        self._last_tool_calls_run_id = normalized_run_id or None
        self._last_usage = dict(seed_usage)
        self._last_usage_run_id = normalized_run_id or None
        ledger = UnifiedTurnLedger(
            run_id=normalized_run_id or None,
            results=seed_results,
            usage=seed_usage,
            failure=seed_failure,
            on_result=self._record_tool_result,
        )
        return ledger

    def _handle_tool_calls(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        thread_id = threading.get_ident()
        with self._generation_owner_lock:
            owned_by_current_thread = self._generation_owner_thread_id == thread_id
        if owned_by_current_thread:
            return self._handle_tool_calls_locked(*args, **kwargs)
        self._acquire_generation_state()
        try:
            return self._handle_tool_calls_locked(*args, **kwargs)
        finally:
            self._release_generation_state()

    def _handle_tool_calls_locked(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        registry: "ToolRegistry",
        max_rounds: int = 5,
        user_input: Optional[str] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
        turn_ledger: UnifiedTurnLedger | None = None,
    ) -> str:
        effective_max_rounds = max(max_rounds, agentic_max_rounds(self, user_input))
        ledger = turn_ledger or UnifiedTurnLedger(on_result=self._record_tool_result)
        with activate_unified_turn_ledger(ledger):
            result = run_openai_tool_call_loop(
                initial_messages=messages,
                assistant_message=assistant_message,
                api_kwargs=api_kwargs,
                registry=registry,
                create_completion=lambda kwargs: self._create_turn_completion(
                    kwargs,
                    ledger,
                    request_type="tool",
                ),
                log_prefix="SGLangClient",
                max_rounds=effective_max_rounds,
                return_result=True,
                config=self.config,
                user_input=user_input,
                event_callback=event_callback,
                restore_tool_arguments=self._privacy_gateway.restore_tool_arguments,
            )
        if ledger.results:
            self._last_tool_calls = [
                self._tool_call_record(tool_result)
                for tool_result in ledger.results
            ]
        else:
            # Keep compatibility with tests and alternate wrappers that return
            # AgentRun-ready records without using the unified ledger.
            audit_results = getattr(result, "audit_tool_results", None)
            if audit_results is None:
                audit_results = getattr(result, "tool_results", None)
            if audit_results is not None:
                self._last_tool_calls.extend(
                    self._tool_call_record(tool_result)
                    for tool_result in audit_results
                )
            else:
                self._last_tool_calls.extend(result.tool_calls)
        self._last_tool_loop_messages = [
            dict(message) for message in getattr(result, "messages", [])
        ]
        return guard_tool_execution_claims(result.final_output, result.tool_calls)

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        user_input: str,
        turn_ledger: UnifiedTurnLedger,
    ) -> str:
        messages = self._build_messages(prompt)
        if not self.send_thinking_control:
            effective_temperature = temperature
            effective_top_p = 0.8
            extra_body = {}
        elif self._thinking_mode:
            effective_temperature = 0.6
            effective_top_p = 0.95
            extra_body = {"chat_template_kwargs": {"enable_thinking": True}}
        else:
            effective_temperature = temperature
            effective_top_p = 0.8
            extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

        registry = filtered_registry_for_client(self, self._tool_registry)
        api_tools = OpenAIAPIAdapter.convert_all(registry.get_all()) if len(registry) > 0 else None
        api_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": effective_temperature,
            "top_p": effective_top_p,
            "max_tokens": max_tokens or 1024,
        }
        if api_tools:
            api_kwargs["tools"] = api_tools
            api_kwargs["tool_choice"] = "auto"
        try:
            response = self._create_turn_completion(
                api_kwargs,
                turn_ledger,
                extra_body=extra_body,
                request_type="review",
            )
        except Exception as api_err:
            if "chat_template" in str(api_err).lower() or "extra_body" in str(api_err).lower():
                logger.warning(f"[SGLangClient] Retrying agentic review without extra_body: {api_err}")
                response = self._create_turn_completion(
                    api_kwargs,
                    turn_ledger,
                    request_kind="chat.completions.retry",
                    request_type="retry",
                )
            else:
                raise

        choice = response.choices[0]
        if choice.message.tool_calls:
            return self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                registry,
                user_input=user_input,
                turn_ledger=turn_ledger,
            )
        response_text = choice.message.content or ""
        if self._thinking_mode:
            response_text, _ = self._process_thinking_response(response_text)
        return guard_tool_execution_claims(response_text, [])

    def _stream_response(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
        user_input: str,
        top_p: float = 0.8,
        extra_body: Optional[Dict[str, Any]] = None,
        *,
        turn_ledger: UnifiedTurnLedger | None = None,
    ) -> Generator[str, None, None]:
        """Stream response from API

        Args:
            messages: Message list
            temperature: Temperature
            max_tokens: Max tokens
            user_input: Original user input for history
            top_p: Top-p sampling parameter
            extra_body: Additional request body parameters (e.g., chat_template_kwargs)

        Yields:
            Response chunks
        """
        yielded_content = False
        ledger = turn_ledger or UnifiedTurnLedger()
        stream_usage: dict[str, Any] = {}
        stream_usage_recorded = False
        try:
            # Try with extra_body first
            try:
                stream = self._create_observed_completion(
                    {
                        "model": self.model_name,
                        "messages": messages,
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens or 1024,
                        "stream": True,
                    },
                    request_kind="chat.completions.stream",
                    extra_body=extra_body or {},
                )
            except Exception as api_err:
                # If extra_body caused an error, retry without it
                if "chat_template" in str(api_err).lower() or "extra_body" in str(api_err).lower():
                    logger.warning(f"[SGLangClient] Model doesn't support chat_template_kwargs in stream, retrying without: {api_err}")
                    stream = self._create_observed_completion(
                        {
                            "model": self.model_name,
                            "messages": messages,
                            "temperature": temperature,
                            "top_p": top_p,
                            "max_tokens": max_tokens or 1024,
                            "stream": True,
                        },
                        request_kind="chat.completions.stream_retry",
                    )
                else:
                    raise

            full_response = ""
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yielded_content = True
                    yield content
                captured_usage = self._capture_usage(chunk)
                if captured_usage:
                    stream_usage = captured_usage

            ledger.record_usage(stream_usage)
            stream_usage_recorded = True
            self._last_usage_run_id = str(ledger.run_id or "").strip() or None

            # Add to history after streaming is complete
            self._record_model_transcript(messages, full_response)
            persist_usage_sync(
                self,
                provider="sglang",
                model=self.model_name,
                usage=stream_usage,
                is_streaming=True,
            )
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", full_response)
            self._complete_tool_turn_attempt(ledger)

        except GenerationInterrupted:
            self.seed_next_tool_turn_attempt(ledger)
            raise
        except Exception as e:
            ledger.failure = f"{type(e).__name__}: {e}"
            if not stream_usage_recorded:
                ledger.record_usage(stream_usage)
            self._last_usage_run_id = str(ledger.run_id or "").strip() or None
            logger.error(f"[SGLangClient] ストリーミングエラー: {e}")
            if yielded_content:
                self._complete_tool_turn_attempt(ledger)
                raise
            self._complete_tool_turn_attempt(ledger)
            yield self._get_fallback_response()

    def _get_fallback_response(self) -> str:
        """Get fallback response for errors"""
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            personality = character_config.get('personality', {})
            return personality.get('fallbackReply', 'すみません、エラーが発生しました。')
        return 'すみません、エラーが発生しました。'

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
        steering_callback: Any = None,
    ) -> str:
        from ..services.session_llm_generation import run_session_aware_generation

        return await run_session_aware_generation(
            self,
            self.config,
            user_input,
            temperature=temperature,
            max_tokens=max_tokens,
            image_data=image_data,
            stream_callback=stream_callback,
            steering_callback=steering_callback,
        )

    async def _generate_response_async_impl(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
        steering_callback: Any = None,
    ) -> str:
        return await asyncio.to_thread(
            self.generate_response,
            user_input,
            temperature,
            max_tokens,
            False,
            image_data,
            stream_callback,
            steering_callback,
        )

    def clear_history(self):
        """Clear conversation history"""
        self.history_manager.clear()
        logger.info(f"[SGLangClient] 会話履歴をクリア")

    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history"""
        return self.history_manager.get_all()

    async def cleanup(self):
        """Clean up resources including stopping the SGLang server if started by this client"""
        with self._pending_tool_turn_results_lock:
            self._pending_tool_turn_results.clear()
            self._completed_tool_turn_results.clear()
            self._last_tool_calls = []
            self._last_tool_calls_run_id = None
            self._last_usage = {}
            self._last_usage_run_id = None
        for stream in list(getattr(self, "_active_streams", ())):
            try:
                stream.close()
            except Exception:
                logger.debug("[SGLangClient] active stream close failed", exc_info=True)
        with self._steering_callback_lock:
            self._steering_callback_run_ids.clear()
        if self._server_started_by_client:
            logger.info("[SGLangClient] クライアントが起動したサーバーを停止中...")
            await self.server_manager.stop()

        logger.info(f"[SGLangClient] クリーンアップ完了")


def create_sglang_client(config: Config) -> SGLangClient:
    """Factory function to create SGLang client with automatic server management

    Args:
        config: Application configuration

    Returns:
        Configured SGLangClient instance
    """
    # Create server manager
    server_manager = SGLangServerManager(config)

    # Get configuration values
    sglang_config = config.get('sglang', {}) or {}
    response_model = (
        config.get('llm_model')
        if config.get('response_model_selection_active')
        else None
    )
    base_url = resolve_sglang_base_url(config)
    model = enforce_enterprise_sglang_model(
        config,
        "sglang",
        resolve_sglang_model(config, response_model=response_model),
    )
    api_key = (
        config.get("runtime.target_api_key")
        or config.get("sglang_api_key")
        or os.getenv("SGLANG_API_KEY")
        or "dummy"
    )

    client = SGLangClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config=config,
        server_manager=server_manager
    )

    # Try to start server in background (non-blocking)
    # The actual blocking wait will happen on first request if needed
    if server_manager.auto_start and not config.get("runtime.defer_server_start", False):
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule for later
                asyncio.ensure_future(client.ensure_server_running())
            else:
                # Run now
                loop.run_until_complete(client.ensure_server_running())
        except RuntimeError:
            # No event loop, create one
            asyncio.run(client.ensure_server_running())

    return client
