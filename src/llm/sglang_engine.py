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
import aiohttp
from typing import Optional, List, Dict, Any, Union, Generator
from pathlib import Path

from openai import OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..tools.registry import get_registry
from ..tools.adapters import OpenAIAPIAdapter

logger = logging.getLogger(__name__)


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

        self.model = sglang_config.get('model') or config.get('sglang_model', os.getenv('SGLANG_MODEL', 'default')) if config else os.getenv('SGLANG_MODEL', 'default')
        self.port = sglang_config.get('port', 30000)
        self.host = sglang_config.get('host', '0.0.0.0')
        self.mem_fraction_static = sglang_config.get('mem_fraction_static', 0.9)
        self.tensor_parallel_size = sglang_config.get('tensor_parallel_size', 1)
        self.max_model_len = sglang_config.get('max_model_len')  # None = auto
        self.dtype = sglang_config.get('dtype', 'auto')
        self.auto_start = sglang_config.get('auto_start', True)

        # Health check settings
        self.startup_timeout = sglang_config.get('startup_timeout', 300)  # 5 minutes
        self.health_check_interval = sglang_config.get('health_check_interval', 5)

        logger.info(f"[SGLangServerManager] 初期化: model={self.model}, port={self.port}, auto_start={self.auto_start}")

    @property
    def base_url(self) -> str:
        """Get the base URL for the SGLang server"""
        return f"http://localhost:{self.port}/v1"

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
        """Check if the SGLang server is ready to accept requests"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://localhost:{self.port}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return response.status == 200
        except Exception:
            pass

        # Try /v1/models endpoint as fallback
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://localhost:{self.port}/v1/models",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return response.status == 200
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

        # Create log directory
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        # Open log files
        stdout_log = open(log_dir / "sglang_server.log", "a", encoding="utf-8")
        stderr_log = open(log_dir / "sglang_server_error.log", "a", encoding="utf-8")

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
            "--tp", str(self.tensor_parallel_size),
            "--dtype", self.dtype,
        ]

        if self.max_model_len:
            cmd.extend(["--max-model-len", str(self.max_model_len)])

        # Add trust-remote-code for HuggingFace models
        cmd.append("--trust-remote-code")

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
        self.model_name = model
        self.api_key = api_key

        # Server manager for automatic startup
        self.server_manager = server_manager or SGLangServerManager(config)

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

        # Initialize history manager
        self.history_manager = HistoryManager()

        # Session context
        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}

        # Build system prompt
        self.system_prompt = self._build_system_prompt()

        # Track if server was started by this client
        self._server_started_by_client = False

        # Thinking mode (Qwen3 specific)
        self._thinking_mode = False  # Default: fast mode

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

    def _build_messages(self, user_input: str) -> List[Dict[str, str]]:
        """Build messages list for API call

        Args:
            user_input: Current user input

        Returns:
            List of message dicts for OpenAI API
        """
        messages = [
            {"role": "system", "content": self.system_prompt}
        ]

        # Add conversation history
        history = self.history_manager.get_all()
        context_window = self.history_manager.context_window_size

        for msg in history[-(context_window * 2):]:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Add current user message
        messages.append({"role": "user", "content": user_input})

        return messages

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None
    ) -> Union[str, Generator[str, None, None]]:
        """Generate response using SGLang/Local LLM

        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            stream: Whether to stream response
            image_data: Optional image data (not supported for SGLang text models, will be ignored)

        Returns:
            Generated response text or generator
        """
        if image_data:
            logger.warning("[SGLangClient] 画像入力はこのプロバイダーでサポートされていません。画像は無視されます。")
        try:
            # Build messages
            messages = self._build_messages(user_input)

            # Mode-specific parameters (Qwen3 thinking mode support)
            if self._thinking_mode:
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
            registry = get_registry()
            api_tools = OpenAIAPIAdapter.convert_all(registry.get_all()) if len(registry) > 0 else None

            # Make API call
            if stream:
                return self._stream_response(messages, effective_temperature, max_tokens, user_input, effective_top_p, extra_body)
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

                # Try with extra_body first, fallback without if model doesn't support it
                try:
                    response = self.client.chat.completions.create(
                        **api_kwargs, extra_body=extra_body
                    )
                except Exception as api_err:
                    if "chat_template" in str(api_err).lower() or "extra_body" in str(api_err).lower():
                        logger.warning(f"[SGLangClient] Retrying without extra_body: {api_err}")
                        response = self.client.chat.completions.create(**api_kwargs)
                    else:
                        raise

                choice = response.choices[0]

                # Handle tool calls if present
                if choice.message.tool_calls:
                    response_text = self._handle_tool_calls(
                        messages, choice.message, api_kwargs, registry
                    )
                else:
                    raw_response_text = choice.message.content or ""
                    if self._thinking_mode:
                        response_text, _ = self._process_thinking_response(raw_response_text)
                    else:
                        response_text = raw_response_text

                # Add to history
                self.history_manager.add("user", user_input)
                self.history_manager.add("assistant", response_text)

                logger.info(f"[SGLangClient] 応答生成完了: {len(response_text)}文字")
                return response_text

        except Exception as e:
            logger.error(f"[SGLangClient] エラー: {e}")
            import traceback
            traceback.print_exc()

            fallback = self._get_fallback_response()
            self.history_manager.add("user", user_input)
            self.history_manager.add("assistant", fallback)

            if stream:
                def error_generator():
                    yield fallback
                return error_generator()
            return fallback

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        registry: "ToolRegistry",
        max_rounds: int = 5
    ) -> str:
        """Handle tool calls from model response

        Executes tools and re-prompts the model with results until a text
        response is produced or max_rounds is reached.
        """
        import json as _json

        current_messages = list(messages)
        current_tool_calls = assistant_message.tool_calls
        current_content = assistant_message.content

        for round_num in range(max_rounds):
            # Append assistant message with tool calls
            current_messages.append({
                "role": "assistant",
                "content": current_content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in current_tool_calls
                ],
            })

            # Execute each tool call
            for tc in current_tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = _json.loads(tc.function.arguments)
                except _json.JSONDecodeError:
                    fn_args = {}

                try:
                    result = registry.execute(fn_name, **fn_args)
                    result_str = str(result)
                except Exception as e:
                    result_str = f"Error: {e}"

                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

                logger.info(f"[SGLangClient] Tool {fn_name} → {result_str[:100]}")

            # Re-call the model with tool results
            follow_up_kwargs = dict(api_kwargs)
            follow_up_kwargs["messages"] = current_messages
            response = self.client.chat.completions.create(**follow_up_kwargs)
            choice = response.choices[0]

            if choice.message.tool_calls:
                current_tool_calls = choice.message.tool_calls
                current_content = choice.message.content
                continue
            else:
                return choice.message.content or ""

        # Exceeded max rounds
        logger.warning("[SGLangClient] Tool call loop exceeded max rounds")
        return current_content or ""

    def _stream_response(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
        user_input: str,
        top_p: float = 0.8,
        extra_body: Optional[Dict[str, Any]] = None
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
        try:
            # Try with extra_body first
            try:
                stream = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens or 1024,
                    stream=True,
                    extra_body=extra_body or {}
                )
            except Exception as api_err:
                # If extra_body caused an error, retry without it
                if "chat_template" in str(api_err).lower() or "extra_body" in str(api_err).lower():
                    logger.warning(f"[SGLangClient] Model doesn't support chat_template_kwargs in stream, retrying without: {api_err}")
                    stream = self.client.chat.completions.create(
                        model=self.model_name,
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens or 1024,
                        stream=True
                    )
                else:
                    raise

            full_response = ""
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield content

            # Add to history after streaming is complete
            self.history_manager.add("user", user_input)
            self.history_manager.add("assistant", full_response)

        except Exception as e:
            logger.error(f"[SGLangClient] ストリーミングエラー: {e}")
            yield self._get_fallback_response()

    def _get_fallback_response(self) -> str:
        """Get fallback response for errors"""
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            personality = character_config.get('personality', {})
            return personality.get('fallbackReply', 'すみません、エラーが発生しました。')
        return 'すみません、エラーが発生しました。'

    def clear_history(self):
        """Clear conversation history"""
        self.history_manager.clear()
        logger.info(f"[SGLangClient] 会話履歴をクリア")

    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history"""
        return self.history_manager.get_all()

    async def cleanup(self):
        """Clean up resources including stopping the SGLang server if started by this client"""
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
    base_url = config.get('sglang_base_url', os.getenv('SGLANG_BASE_URL', server_manager.base_url))
    model = sglang_config.get('model') or config.get('sglang_model', os.getenv('SGLANG_MODEL', config.get('llm_model', 'default')))
    api_key = config.get('sglang_api_key', os.getenv('SGLANG_API_KEY', 'dummy'))

    client = SGLangClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config=config,
        server_manager=server_manager
    )

    # Try to start server in background (non-blocking)
    # The actual blocking wait will happen on first request if needed
    if server_manager.auto_start:
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
