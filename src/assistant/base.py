"""
Base assistant class for AoiTalk Voice Assistant Framework
"""

import asyncio
import time
import platform
import os
import threading
import logging
from urllib.parse import urlparse
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pathlib import Path
from src.tools.keyword.character_manager import get_character_manager
from src.features import Features
from src.utils.startup_timing import get_startup_timer
from src.utils.startup_console import safe_startup_reason, startup_console
from src.utils.logging_config import FILE_ONLY_LOG_EXTRA


_startup_timer = get_startup_timer()
logger = logging.getLogger(__name__)


def _startup_detail_extra() -> dict[str, bool]:
    """Hide routine startup internals from normal console output.

    Debug mode intentionally keeps these records visible on the console while
    normal mode retains them in the application log for diagnosis.
    """

    return {} if os.getenv("AOITALK_DEBUG", "").strip().lower() == "true" else FILE_ONLY_LOG_EXTRA

# WSL2環境の自動設定
if platform.system() == 'Linux':
    try:
        with open('/proc/version', 'r') as f:
            if 'microsoft' in f.read().lower():
                from dotenv import load_dotenv
                load_dotenv()
                
                pulse_runtime_path = os.getenv('PULSE_RUNTIME_PATH', '/mnt/wslg/runtime-dir/pulse')
                if os.path.exists(pulse_runtime_path):
                    os.environ['PULSE_RUNTIME_PATH'] = pulse_runtime_path
                
                os.environ['SDL_AUDIODRIVER'] = 'pulse'
    except:
        pass


class BaseAssistant(ABC):
    """Base class for all assistant modes"""
    
    def __init__(self, config, mode: str):
        """Initialize base assistant
        
        Args:
            config: Configuration object
            mode: Assistant mode ('terminal', 'voice_chat', etc.)
        """
        self.config = config
        self.mode = mode
        self.running = False
        self.web_interface = None
        self._web_interface_ready_callback = None
        self._startup_failed = False
        # Keep the exact manager/callback pair owned by this assistant.  The
        # character manager is process-wide, so resolving it again during
        # cleanup could unregister from a different manager in tests or after
        # a runtime reconfiguration.
        self._character_switch_manager = None
        self._character_switch_callback = None
        self._character_callback_unregistered = False

        # Memory initialization is intentionally allowed to outlive the short
        # startup wait.  Keep the task reference so assistant cleanup can
        # cancel and await it before tearing down the client it uses.
        self._memory_init_task: asyncio.Task | None = None

        # cleanup() can be reached from more than one shutdown path (for
        # example a mode runner and a supervisor).  Serialize it and make the
        # operation idempotent.
        self._cleanup_lock: asyncio.Lock | None = None
        self._cleanup_started = False
        self._cleanup_done = False
        
        # Load character configuration
        self.character_name = self.config.default_character
        self.character_config = self.config.get_character_config(self.character_name)
        
        try:
            # Register character switch callback before common components are
            # initialized so character changes are observed immediately.
            self._register_character_switch_callback()

            # Common initialization
            self._init_common_components()
        except BaseException:
            # Callback registration happens before common components are
            # created.  Roll it back if client setup fails so a partially
            # constructed assistant cannot remain in the process-wide
            # manager's callback list.
            self._unregister_character_switch_callback()
            raise
        
    def _init_common_components(self):
        """Initialize components common to all modes"""
        logger.info("[BaseAssistant] _init_common_components開始", extra=_startup_detail_extra())
        
        # LLM client initialization
        from src.llm.manager import create_llm_client
        
        use_tools = self.config.get('use_tools', True)
        if use_tools:
            logger.info("[ツールモード] Function calling・MCP対応", extra=_startup_detail_extra())
        else:
            logger.info("[標準モード] 基本的なLLMクライアントを使用します", extra=_startup_detail_extra())

        logger.info("[BaseAssistant] LLMクライアント作成開始", extra=_startup_detail_extra())
        self.llm_client = create_llm_client(self.config)
        logger.info("[BaseAssistant] LLMクライアント作成完了", extra=_startup_detail_extra())
        
        # Set LLM system prompt
        personality = self.character_config.get('personality', {})
        system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
        self.llm_client.set_system_prompt(system_prompt)
        logger.info("[BaseAssistant] _init_common_components完了", extra=_startup_detail_extra())

    def _activate_llm_client(self, llm_client):
        """Replace the LLM client used by assistant response generation."""
        self.llm_client = llm_client

        personality = self.character_config.get('personality', {})
        system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
        if hasattr(self.llm_client, 'set_system_prompt'):
            self.llm_client.set_system_prompt(system_prompt)

        if hasattr(self, 'response_handler') and self.response_handler:
            self.response_handler.llm_client = self.llm_client

        if self.web_interface and hasattr(self.llm_client, 'clear_history'):
            self.web_interface.set_clear_chat_callback(self.llm_client.clear_history)

    async def initialize(self) -> bool:
        """Initialize assistant components
        
        Returns:
            bool: True if initialization succeeded
        """
        _initialize_phase = _startup_timer.start_phase("startup.assistant.initialize")
        if getattr(self, "_cleanup_started", False):
            # Do not reacquire mode or memory resources after this owner has
            # begun teardown.  Returning False preserves the existing bool
            # initialization contract while keeping cleanup idempotent.
            logger.warning("[BaseAssistant] cleanup has started; initialization refused")
            _startup_timer.finish_phase(_initialize_phase, status="skipped")
            return False
        try:
            logger.info(
                "初期化中... (キャラクター: %s)",
                self.character_name,
                extra=_startup_detail_extra(),
            )
            logger.info("[BaseAssistant] initializeメソッド開始", extra=_startup_detail_extra())

            # Initialize memory manager in background to avoid blocking startup
            memory_init_task = None
            llm_client = getattr(self, "llm_client", None)
            memory_manager = getattr(llm_client, "memory_manager", None)
            existing_memory_task = getattr(self, "_memory_init_task", None)
            if (
                existing_memory_task is not None
                and not existing_memory_task.done()
            ):
                # initialize() can be called by a supervisor retry path.  Do
                # not orphan an earlier in-flight memory task by replacing its
                # reference; reuse the owned task instead.
                memory_init_task = existing_memory_task
            elif (
                not getattr(self, "_cleanup_started", False)
                and memory_manager
                and hasattr(memory_manager, 'initialize')
            ):
                async def init_memory_background():
                    try:
                        logger.info(
                            "[BaseAssistant] メモリシステムをバックグラウンドで初期化中...",
                            extra=_startup_detail_extra(),
                        )
                        # Capture the manager at task creation time.  The
                        # active LLM client can be swapped while startup
                        # continues; memory initialization belongs to the
                        # client that supplied this manager.
                        await memory_manager.initialize()
                        logger.info(
                            "[BaseAssistant] メモリシステムの初期化完了",
                            extra=_startup_detail_extra(),
                        )
                    except Exception as e:
                        logger.warning(
                            "[BaseAssistant] メモリシステムの初期化エラー: %s",
                            e,
                            extra=FILE_ONLY_LOG_EXTRA,
                        )

                # Start memory initialization in background.  Keep the
                # reference even if the short startup wait times out;
                # cleanup() owns and awaits this task later.
                memory_init_task = asyncio.create_task(
                    init_memory_background(),
                    name=f"{type(self).__name__}.memory_init",
                )
                self._memory_init_task = memory_init_task

            # Mode-specific initialization (e.g., VOICEVOX) runs in parallel
            mode_init_result = await self._initialize_mode_specific()

            # Optionally wait for memory init with a short timeout
            if memory_init_task:
                try:
                    # wait_for(task) cancels the task on timeout.  Shield the
                    # owned task so initialization may continue in the
                    # background and be explicitly stopped during cleanup.
                    await asyncio.wait_for(
                        asyncio.shield(memory_init_task),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    # Memory init continues in background
                    logger.info(
                        "[BaseAssistant] メモリシステムの初期化は継続中（バックグラウンド）",
                        extra=_startup_detail_extra(),
                    )
        except BaseException:
            # Roll back a memory initialization task if mode startup fails or
            # the assistant itself is cancelled.  Waiting for the task's
            # completion keeps its finally/transport cleanup on the live loop.
            task = getattr(self, "_memory_init_task", None)
            if task is not None:
                if not task.done():
                    task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None:
                            uncancel = getattr(current, "uncancel", None)
                            if callable(uncancel):
                                uncancel()
                        continue
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
                except Exception as task_error:
                    logger.warning(
                        "[BaseAssistant] memory init rollback failed: %s",
                        task_error,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
                self._memory_init_task = None
            _startup_timer.finish_phase(_initialize_phase, status="error")
            raise

        _startup_timer.finish_phase(_initialize_phase)
        return mode_init_result

    def _get_web_interface_settings(self):
        """Resolve WebUI host/port/auto-open settings from config/env"""
        web_config = self.config.get('web_interface', {}) or {}

        host = web_config.get('host', '127.0.0.1') or '127.0.0.1'
        port = web_config.get('port', 3000) or 3000
        auto_open = web_config.get('auto_open_browser', True)

        host = os.getenv('AOITALK_WEB_HOST', host)

        env_port_name = (
            'AOITALK_WEB_PORT'
            if os.getenv('AOITALK_WEB_PORT')
            else 'AOITALK_FASTAPI_PORT'
        )
        env_port = os.getenv(env_port_name)
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                logger.warning(
                    "[WebUI] Invalid %s '%s', falling back to %s",
                    env_port_name,
                    env_port,
                    port,
                )

        env_auto = os.getenv('AOITALK_WEB_AUTO_OPEN')
        if env_auto is not None:
            auto_open = env_auto.strip().lower() in ('1', 'true', 'yes', 'on')

        try:
            port = int(port)
        except (TypeError, ValueError):
            logger.warning(
                "[WebUI] Invalid port setting '%s', falling back to 3000",
                port,
            )
            port = 3000

        return host, port, bool(auto_open)

    def _get_local_browser_url(self, host: str, port: int, server_url: str) -> str:
        """Translate wildcard hosts to a usable loopback URL for auto-open"""
        wildcard_hosts = {'0.0.0.0', '::', '[::]'}
        host_value = str(host).strip()
        
        # Determine protocol from server_url
        protocol = "https" if server_url.startswith("https://") else "http"

        try:
            parsed_url = urlparse(str(server_url))
            advertised_host = (parsed_url.hostname or "").strip().lower()
        except Exception:
            advertised_host = ""

        # Only rewrite the FastAPI wildcard listener.  A Caddy callback may
        # intentionally return a real public hostname while FastAPI itself is
        # bound to 0.0.0.0; replacing that URL would hide the operator-facing
        # proxy endpoint and open the wrong port.
        if host_value in wildcard_hosts and advertised_host in {
            "",
            "0.0.0.0",
            "::",
            "[::]",
        }:
            return f"{protocol}://127.0.0.1:{port}"

        # Additional safeguard: uvicorn may report the bound URL as 0.0.0.0 even if
        # host string had extra formatting. Rewrite those cases as well.
        if advertised_host in {"0.0.0.0", "::", "[::]"}:
            return f"{protocol}://127.0.0.1:{port}"

        return server_url

    def _get_ssl_settings(self):
        """Resolve SSL settings from environment variables
        
        Returns:
            tuple: (ssl_enabled, ssl_keyfile, ssl_certfile)
        """
        ssl_enabled = os.getenv('AOITALK_SSL_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
        ssl_keyfile = os.getenv('AOITALK_SSL_KEYFILE', '')
        ssl_certfile = os.getenv('AOITALK_SSL_CERTFILE', '')
        
        if ssl_enabled:
            # Resolve relative paths from project root
            project_root = Path(__file__).parent.parent.parent
            
            if ssl_keyfile and not Path(ssl_keyfile).is_absolute():
                ssl_keyfile = str(project_root / ssl_keyfile)
            if ssl_certfile and not Path(ssl_certfile).is_absolute():
                ssl_certfile = str(project_root / ssl_certfile)
            
            # Verify files exist
            if not Path(ssl_keyfile).exists():
                logger.warning("SSL key file not found: %s", ssl_keyfile)
                logger.warning(
                    "Set AOITALK_SSL_KEYFILE and AOITALK_SSL_CERTFILE to existing certificate files"
                )
                return False, None, None
            if not Path(ssl_certfile).exists():
                logger.warning("SSL cert file not found: %s", ssl_certfile)
                logger.warning(
                    "Set AOITALK_SSL_KEYFILE and AOITALK_SSL_CERTFILE to existing certificate files"
                )
                return False, None, None
        
        return ssl_enabled, ssl_keyfile if ssl_enabled else None, ssl_certfile if ssl_enabled else None

    def _start_web_interface(self, input_callback, host: str = '127.0.0.1', port: int = 3000,
                              auto_open_browser: bool = True) -> Optional[str]:
        """Start FastAPI-based web interface shared across modes"""
        try:
            from src.api.web_interface import create_web_interface
        except ImportError as exc:
            reason = (
                "Webインターフェースの依存関係が不足しています: "
                f"{safe_startup_reason(exc)}"
            )
            startup_console.error(reason)
            logger.error(
                "Web interface dependencies are unavailable; install fastapi, uvicorn[standard], and websockets",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            require_runtime = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
                "1", "true", "yes", "on"
            } or Features.is_enterprise()
            if require_runtime:
                self._startup_failed = True
                raise RuntimeError(reason) from exc
            return None

        try:
            # Get SSL settings
            ssl_enabled, ssl_keyfile, ssl_certfile = self._get_ssl_settings()
            
            self.web_interface = create_web_interface(self.config, self.character_name)
            current_loop = asyncio.get_running_loop()
            self.web_interface.set_user_input_callback(input_callback, current_loop)
            self.web_interface.set_llm_client_change_callback(self._activate_llm_client)
            self.web_interface.set_llm_client(self.llm_client)

            # Set clear chat callback to start new session when user clicks "New Conversation"
            if hasattr(self.llm_client, 'clear_history'):
                self.web_interface.set_clear_chat_callback(self.llm_client.clear_history)
            
            with _startup_timer.phase("startup.web.fastapi.start"):
                server_url = self.web_interface.start_server(
                    host=host, port=port,
                    ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile
                )
        except Exception as e:
            startup_console.error(
                "Webインターフェースの開始に失敗しました: "
                f"{safe_startup_reason(e)}"
            )
            logger.exception(
                "Web interface startup failed",
                extra=FILE_ONLY_LOG_EXTRA,
            )
            require_runtime = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
                "1", "true", "yes", "on"
            } or Features.is_enterprise()
            if require_runtime:
                raise
            return None

        if not server_url:
            failure = getattr(self.web_interface, "startup_failure", None)
            failure_message = getattr(failure, "message", None)
            startup_console.error(
                failure_message
                if isinstance(failure_message, str) and failure_message.strip()
                else f"Webインターフェースを起動できませんでした: {host}:{port}"
            )
            require_runtime = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
                "1", "true", "yes", "on"
            } or Features.is_enterprise()
            if require_runtime:
                raise RuntimeError(
                    failure_message
                    if isinstance(failure_message, str) and failure_message.strip()
                    else f"Enterprise WebUI did not become ready on {host}:{port}"
                )
            return None
        # FastAPI readiness is the backend stage.  Caddy is started only after
        # that readiness signal and may return the actual public URL.
        startup_console.stage("[OK] Backend")
        public_url: str | None = None
        if self._web_interface_ready_callback:
            try:
                with _startup_timer.phase("startup.services.caddy.start"):
                    callback_result = self._web_interface_ready_callback(host, port)
                if isinstance(callback_result, str) and callback_result.strip():
                    public_url = callback_result.strip()
                else:
                    callback_url = getattr(callback_result, "public_url", None)
                    if isinstance(callback_url, str) and callback_url.strip():
                        public_url = callback_url.strip()
            except Exception as exc:
                self.web_interface.stop_server()
                self._startup_failed = True
                # The FastAPI listener was healthy, but the required Caddy
                # boundary could not be established. Keep the cause visible
                # to the operator and let ``main`` return a non-zero exit
                # status instead of silently degrading to a false Ready state.
                logger.exception(
                    "Caddy startup failed after FastAPI became ready",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
                startup_console.error(
                    "Caddyの起動に失敗しました: "
                    f"{safe_startup_reason(exc)}"
                )
                raise

        browser_url = self._get_local_browser_url(
            host,
            port,
            public_url or server_url,
        )
        # Emit the final operator-facing summary exactly once.  This owns the
        # detailed app/startup log paths and elapsed time; mode runners should
        # not duplicate URL/path diagnostics.
        startup_console.ready(web_url=browser_url)
        if auto_open_browser:
            self._open_browser_async(browser_url)
        return browser_url

    def set_web_interface_ready_callback(self, callback) -> None:
        """Register orchestration to run after FastAPI reports readiness."""
        self._web_interface_ready_callback = callback

    def _open_browser_async(self, server_url: str):
        """Open browser asynchronously to avoid blocking event loop"""
        def open_browser():
            import time as _time
            _time.sleep(1.5)
            try:
                # Special handling for WSL2
                if platform.system() == 'Linux' and 'microsoft' in platform.uname().release.lower():
                    import subprocess
                    subprocess.run(['cmd.exe', '/c', 'start', server_url], check=True)
                    logger.info(
                        "ブラウザを自動で開きました: %s",
                        server_url,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
                else:
                    import webbrowser
                    webbrowser.open(server_url)
                    logger.info(
                        "ブラウザを自動で開きました: %s",
                        server_url,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
            except Exception as e:
                startup_console.warning(
                    "ブラウザの自動起動に失敗しました: "
                    f"{safe_startup_reason(e)}"
                )
                logger.exception(
                    "Automatic browser launch failed",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
                startup_console.stage(
                    f"手動で以下のURLにアクセスしてください: {server_url}"
                )

        browser_thread = threading.Thread(target=open_browser, daemon=True)
        browser_thread.start()

    @abstractmethod
    async def _initialize_mode_specific(self) -> bool:
        """Initialize mode-specific components
        
        Returns:
            bool: True if initialization succeeded
        """
        pass
    
    @abstractmethod
    async def run(self):
        """Run the assistant"""
        pass
        
    def _register_character_switch_callback(self):
        """Register callback for character switching"""
        manager = get_character_manager()
        callback = self._on_character_switch
        self._character_switch_manager = manager
        self._character_switch_callback = callback
        self._character_callback_unregistered = False
        manager.register_callback(callback)

    def _unregister_character_switch_callback(self) -> None:
        """Release this assistant's character-switch callback once."""
        if getattr(self, "_character_callback_unregistered", False):
            return

        manager = getattr(self, "_character_switch_manager", None)
        callback = getattr(self, "_character_switch_callback", None)
        try:
            if manager is not None and callback is not None:
                try:
                    manager.unregister_callback(callback)
                except Exception as e:
                    # Unregister is best-effort for custom manager doubles;
                    # the owner is still marked released to keep cleanup
                    # idempotent.
                    print(f"キャラクターコールバック解除エラー: {e}")
        finally:
            # Also mark attempted release when a custom manager raises
            # CancelledError (a BaseException) so cleanup cannot loop forever.
            self._character_callback_unregistered = True
        
    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """Handle character switch event
        
        Args:
            character_name: New character name
            yaml_filename: YAML filename (without extension)
        """
        print(f"[BaseAssistant] キャラクター切り替え: {self.character_name} -> {character_name}")
        
        # Update character configuration
        self.character_name = character_name
        self.character_config = self.config.get_character_config(character_name)
        
        # Update LLM client with new character
        llm_client = getattr(self, "llm_client", None)
        if llm_client:
            # Use update_character/set_character if available on the LLM client.
            if hasattr(llm_client, 'update_character'):
                llm_client.update_character(yaml_filename)
                print(f"[BaseAssistant] LLMキャラクターを更新しました (update_character)")
            elif hasattr(llm_client, 'set_character'):
                llm_client.set_character(character_name)
                print(f"[BaseAssistant] LLMキャラクターを更新しました (set_character)")
            else:
                # Fallback: set_system_prompt for non-CLI backends
                personality = self.character_config.get('personality', {})
                system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
                llm_client.set_system_prompt(system_prompt)
                print(f"[BaseAssistant] LLMのシステムプロンプトを更新しました")
        
        # Update the same character manager that owns this callback.  Falling
        # back to the global accessor keeps object.__new__-constructed test
        # doubles and older callers compatible.
        manager = getattr(self, "_character_switch_manager", None)
        if manager is None:
            manager = get_character_manager()
        manager._current_character = character_name
        manager._current_yaml = yaml_filename
        
    async def cleanup(self):
        """Cleanup resources"""
        # asyncio.Lock is created lazily so assistants constructed outside a
        # running loop remain usable.  A lock also prevents concurrent
        # shutdown paths from cancelling/closing the same resources twice.
        cleanup_lock = getattr(self, "_cleanup_lock", None)
        if cleanup_lock is None:
            cleanup_lock = asyncio.Lock()
            self._cleanup_lock = cleanup_lock

        async with cleanup_lock:
            if getattr(self, "_cleanup_done", False):
                return

            self._cleanup_started = True
            self.running = False
            cleanup_cancelled = False

            # Release the callback registration before the manager can
            # dispatch another switch to a partially torn-down assistant.
            if not getattr(self, "_character_callback_unregistered", False):
                try:
                    self._unregister_character_switch_callback()
                except asyncio.CancelledError:
                    # Keep releasing the remaining resources, then return
                    # cancellation to the caller after cleanup completes.
                    cleanup_cancelled = True

            # Memory initialization may still be using the LLM/memory client.
            # Stop and await it before mode-specific or LLM cleanup.
            memory_init_task = getattr(self, "_memory_init_task", None)
            if memory_init_task is not None:
                if not memory_init_task.done():
                    memory_init_task.cancel()
                # Shield the owned task from cancellation of this cleanup
                # waiter; an outer cancellation is deferred until the task's
                # finally/transport cleanup has completed.
                while not memory_init_task.done():
                    try:
                        await asyncio.shield(memory_init_task)
                    except asyncio.CancelledError:
                        # A CancelledError from a now-completed child is the
                        # expected result of the cancellation requested above;
                        # only an unfinished child means this cleanup waiter
                        # itself was cancelled.
                        if memory_init_task.done():
                            break
                        cleanup_cancelled = True
                        current = asyncio.current_task()
                        if current is not None:
                            uncancel = getattr(current, "uncancel", None)
                            if callable(uncancel):
                                uncancel()
                        continue
                try:
                    await asyncio.shield(memory_init_task)
                except asyncio.CancelledError:
                    # Cancellation is the normal shutdown outcome.
                    pass
                except Exception as e:
                    # Awaiting retrieves exceptions so they cannot surface as
                    # "Task exception was never retrieved" during loop close.
                    print(f"[BaseAssistant] メモリ初期化タスクの終了エラー: {e}")
                finally:
                    self._memory_init_task = None

            # WebUI owns a separate uvicorn thread/event loop. Stop it before
            # mode-specific and LLM cleanup so supervisor/Docker shutdown
            # cannot leave a live listener behind.
            web_interface = getattr(self, "web_interface", None)
            if web_interface and hasattr(web_interface, "stop_server"):
                try:
                    web_interface.stop_server()
                except Exception as e:
                    print(f"WebUIクリーンアップエラー: {e}")

            # Get goodbye message
            character_config = getattr(self, "character_config", {}) or {}
            personality = character_config.get('personality', {})
            goodbye = personality.get('goodbyeReply', 'さようなら！')

            try:
                await self._cleanup_mode_specific()
            except asyncio.CancelledError:
                cleanup_cancelled = True
                print("モード固有クリーンアップがキャンセルされました")
            except Exception as e:
                # Preserve the historical best-effort cleanup behavior while
                # allowing LLM cleanup below to run as well.
                print(f"モード固有クリーンアップエラー: {e}")

            # Cleanup LLM client (including MCP)
            llm_client = getattr(self, "llm_client", None)
            if llm_client is not None and hasattr(llm_client, 'cleanup'):
                try:
                    await llm_client.cleanup()
                except asyncio.CancelledError:
                    cleanup_cancelled = True
                    print("LLMクライアントのクリーンアップがキャンセルされました")
                except Exception as e:
                    print(f"LLMクライアントのクリーンアップエラー: {e}")

            print(f"\n{getattr(self, 'character_name', '')}: {goodbye}")
            self._cleanup_done = True
            if cleanup_cancelled:
                raise asyncio.CancelledError
        
    @abstractmethod
    async def _cleanup_mode_specific(self):
        """Cleanup mode-specific resources"""
        pass
        
    async def _generate_with_interrupt_check(self, text: str, task_id: str = "unknown", parent_task = None) -> Optional[str]:
        """Generate response with task-specific cancellation checking
        
        Args:
            text: Input text
            task_id: Task identifier for logging
            parent_task: Parent asyncio task for cancellation check
            
        Returns:
            Generated response or None if cancelled
        """
        # Check if parent task was cancelled before starting
        if parent_task and parent_task.cancelled():
            print(f"[{task_id}] 親タスクキャンセル済み - 応答生成をスキップ")
            return None
            
        try:
            # Direct async call instead of executor to allow proper cancellation
            if hasattr(self.llm_client, 'generate_response_async'):
                # Use async version if available
                response = await self.llm_client.generate_response_async(text)
            else:
                # Fallback: create a task that can be cancelled
                generation_task = asyncio.create_task(
                    asyncio.to_thread(lambda: self.llm_client.generate_response(text, stream=False))
                )
                
                # Monitor for parent task cancellation during generation
                while not generation_task.done():
                    if parent_task and parent_task.cancelled():
                        print(f"[{task_id}] 応答生成中に親タスクキャンセル検出")
                        generation_task.cancel()
                        try:
                            await generation_task
                        except asyncio.CancelledError:
                            pass
                        return None
                    await asyncio.sleep(0.05)  # Check every 50ms
                
                response = await generation_task
                
            # Final cancellation check
            if parent_task and parent_task.cancelled():
                print(f"[{task_id}] 応答生成完了後に親タスクキャンセル検出")
                return None
                
            return response
            
        except asyncio.CancelledError:
            print(f"[{task_id}] 応答生成タスクがキャンセルされました")
            return None
        except Exception as e:
            print(f"[{task_id}] 応答生成エラー: {e}")
            return None
