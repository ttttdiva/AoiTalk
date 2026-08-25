#!/usr/bin/env python3
"""
FastAPI WebSocket interface wrapper
Provides compatibility layer for existing VoiceChatMode
"""

import asyncio
import socket
import threading
import uvicorn
from pathlib import Path
from .server import create_web_interface as create_fastapi_interface
from src.utils.startup_timing import get_startup_timer


_startup_timer = get_startup_timer()

class WebChatInterface:
    """Wrapper class for FastAPI WebSocket server"""
    
    def __init__(self, config, character_name):
        """Initialize FastAPI wrapper"""
        self.config = config
        self.character_name = character_name
        with _startup_timer.phase("startup.web.fastapi.app_factory"):
            self.server = create_fastapi_interface(config, character_name)
            self.app = self.server.get_app()
        
        # Server state
        self.is_running = False
        self.server_thread = None
        self.uvicorn_server = None
        self.video_http_server = None
        self.video_http_thread = None
        self._server_loop = None  # uvicornスレッドのイベントループ
        self._startup_error: Exception | None = None

        # Expose server methods
        self.add_assistant_message = self._async_wrapper(self.server.add_assistant_message)
        self.add_system_message = self._async_wrapper(self.server.add_system_message)
        self.add_user_message = self._async_wrapper(self.server.add_user_message)
        self.broadcast_stream_event = self._async_wrapper(self.server.broadcast_stream_event)
        self.dispatch_voice_message = self._dispatch_voice_message
        self.set_voice_recognition_ready = self.server.set_voice_recognition_ready
        self.update_rms = self.server.update_rms
        self.set_recording_state = self.server.set_recording_state
        
    def _async_wrapper(self, async_func):
        """Wrap async function for sync/cross-thread calls.

        WebSocket broadcast must run on uvicorn's event loop (the thread that
        owns the ASGI connections).  When called from the main event loop
        (e.g. via run_coroutine_threadsafe), we forward the coroutine to
        _server_loop instead of creating a task on the caller's loop.
        """
        def wrapper(*args, **kwargs):
            # uvicornのイベントループが保存されていれば、そこにスケジュール
            if self._server_loop and self._server_loop.is_running():
                asyncio.run_coroutine_threadsafe(async_func(*args, **kwargs), self._server_loop)
                return
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(async_func(*args, **kwargs))
                else:
                    asyncio.run(async_func(*args, **kwargs))
            except RuntimeError:
                asyncio.run(async_func(*args, **kwargs))
        return wrapper

    def _dispatch_voice_message(self, message: str) -> bool:
        """Queue local voice input into the latest active chat session."""
        if not self.server.get_voice_input_session_id():
            return False
        self._async_wrapper(self.server.dispatch_voice_message)(message)
        return True
        
    def set_user_input_callback(self, callback, event_loop=None):
        """Set user input callback"""
        self.server.set_user_input_callback(callback, event_loop)
    
    def set_clear_chat_callback(self, callback):
        """Set clear chat callback (called when user starts a new conversation)"""
        self.server.set_clear_chat_callback(callback)

    def set_llm_client_change_callback(self, callback):
        """Set callback invoked when the server switches LLM clients."""
        self.server.set_llm_client_change_callback(callback)

    def set_llm_client(self, llm_client):
        """Set the active LLM client on the server."""
        self.server.set_llm_client(llm_client)
    
    def _get_video_http_port(self, main_port: int) -> int:
        """Get video HTTP port from config or default to main_port + 1"""
        try:
            web_config = self.config.config.get('web_interface', {})
            video_config = web_config.get('video_http_server', {})
            # 新形式: video_http_server.port、旧形式: video_http_port をフォールバック
            return video_config.get('port', web_config.get('video_http_port', main_port + 1))
        except Exception:
            return main_port + 1
    
    def _is_video_http_enabled(self) -> bool:
        """Check if HTTP video server is enabled in config"""
        try:
            web_config = self.config.config.get('web_interface', {})
            video_config = web_config.get('video_http_server', {})
            return video_config.get('enabled', False) is True
        except Exception:
            return False

    def _get_video_http_host(self) -> str:
        """Resolve the dedicated helper bind without inheriting a public UI bind."""
        try:
            web_config = self.config.config.get('web_interface', {})
            video_config = web_config.get('video_http_server', {})
            return str(video_config.get('host', '127.0.0.1')).strip()
        except Exception:
            return '127.0.0.1'

    def _get_video_http_allowed_origins(self, main_port: int) -> list[str]:
        """Return explicit local UI origins for the credential-free helper."""
        try:
            web_config = self.config.config.get('web_interface', {})
            video_config = web_config.get('video_http_server', {})
            configured = video_config.get('allowed_origins')
            if configured is not None:
                if isinstance(configured, str):
                    return [configured]
                if isinstance(configured, (list, tuple)):
                    return [str(origin) for origin in configured]
                return []
        except Exception:
            return []
        return [
            f"https://127.0.0.1:{main_port}",
            f"https://localhost:{main_port}",
            f"https://[::1]:{main_port}",
        ]

    def _can_bind(self, host: str, port: int) -> bool:
        """Return whether uvicorn can bind the requested host/port."""
        bind_host = "::" if host == "[::]" else host
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((bind_host, port))
            return True
        except OSError:
            return False
    
    def _start_video_http_server(
        self,
        host: str,
        video_port: int,
        *,
        allowed_origins: list[str] | None = None,
    ):
        """Start HTTP video server in a separate thread"""
        try:
            from .video_http_server import create_video_http_app, is_loopback_host
        except ImportError:
            print("[WebUI] ⚠️ Video HTTP server module not found, skipping")
            return
        if not is_loopback_host(host):
            raise ValueError("Video HTTP server must bind to a loopback host")
        
        def run_video_server():
            try:
                print(f"[WebUI] 🎬 Starting HTTP video server on http://{host}:{video_port}")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                video_app = create_video_http_app(allowed_origins=allowed_origins)
                config = uvicorn.Config(
                    app=video_app,
                    host=host,
                    port=video_port,
                    log_level="warning",
                    access_log=False,
                )
                self.video_http_server = uvicorn.Server(config)
                loop.run_until_complete(self.video_http_server.serve())
            except Exception as e:
                print(f"[WebUI] Video HTTP server error: {e}")
        
        self.video_http_thread = threading.Thread(target=run_video_server, daemon=True)
        self.video_http_thread.start()
        
    def start_server(self, host='127.0.0.1', port=3000, debug=False,
                      ssl_keyfile=None, ssl_certfile=None):
        """Start FastAPI server
        
        Args:
            host: Host address to bind
            port: Port number
            debug: Enable debug logging
            ssl_keyfile: Path to SSL private key file (for HTTPS)
            ssl_certfile: Path to SSL certificate file (for HTTPS)
        """
        use_ssl = ssl_keyfile and ssl_certfile
        protocol = "https" if use_ssl else "http"

        with _startup_timer.phase("startup.web.fastapi.listener_probe"):
            can_bind = self._can_bind(host, port)
        if not can_bind:
            print(
                f"[WebUI] Port {port} is already in use on {host}; "
                "FastAPI server was not started."
            )
            self.is_running = False
            return None

        # Start HTTP video server if using SSL AND enabled in config (for Android compatibility)
        if use_ssl and self._is_video_http_enabled():
            video_port = self._get_video_http_port(port)
            video_host = self._get_video_http_host()
            self._start_video_http_server(
                video_host,
                video_port,
                allowed_origins=self._get_video_http_allowed_origins(port),
            )
        elif use_ssl and not self._is_video_http_enabled():
            print("[WebUI] ℹ️ HTTP video server disabled in config")

        self._startup_error = None

        def run_server():
            loop = None
            try:
                print(f"[WebUI] Starting FastAPI server on {protocol}://{host}:{port}")
                if use_ssl:
                    print(f"[WebUI] 🔐 SSL enabled - using HTTPS")
                # Create new event loop for the thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._server_loop = loop  # broadcast用に保存
                _startup_timer.mark("startup.web.fastapi.uvicorn_thread_started")

                config = uvicorn.Config(
                    app=self.app,
                    host=host,
                    port=port,
                    log_level="warning" if not debug else "info",
                    access_log=False,
                    ssl_keyfile=ssl_keyfile if use_ssl else None,
                    ssl_certfile=ssl_certfile if use_ssl else None,
                    ws_ping_interval=30,
                    ws_ping_timeout=10,
                )
                self.uvicorn_server = uvicorn.Server(config)
                _startup_timer.mark("startup.web.fastapi.uvicorn_configured")
                loop.run_until_complete(self.uvicorn_server.serve())
            except Exception as e:
                self._startup_error = e
                print(f"[WebUI] Server error: {e}")
            finally:
                self._server_loop = None
                if loop is not None:
                    try:
                        loop.close()
                    except Exception:
                        pass
                
        self.is_running = True
        with _startup_timer.phase("startup.web.fastapi.thread_spawn"):
            self.server_thread = threading.Thread(target=run_server, daemon=True)
            self.server_thread.start()

        # Uvicorn が lifespan startup を完了するまで待つ。固定 sleep だけでは、
        # bind 失敗したスレッドを起動成功として Caddy を公開してしまう。
        import time
        with _startup_timer.phase("startup.web.fastapi.readiness_poll"):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.uvicorn_server and self.uvicorn_server.started:
                    # ``Server.started`` is set after sockets are bound and the
                    # ASGI lifespan startup has completed. Keep listener and
                    # HTTP handling milestones distinct in the log even though
                    # Uvicorn exposes them through this single readiness flag.
                    _startup_timer.mark("startup.web.fastapi.listener_ready")
                    _startup_timer.mark("startup.web.fastapi.lifespan_ready")
                    _startup_timer.mark("startup.web.fastapi.http_ready")
                    return f"{protocol}://{host}:{port}"
                if not self.server_thread.is_alive():
                    break
                time.sleep(0.05)

            self.is_running = False
            if self.uvicorn_server:
                self.uvicorn_server.should_exit = True
            if self._startup_error:
                raise RuntimeError(
                    f"FastAPI startup failed on {host}:{port}: {self._startup_error}"
                ) from self._startup_error
            print(
                f"[WebUI] FastAPI server did not become ready on "
                f"{protocol}://{host}:{port}"
            )
            return None
        
    def stop_server(self):
        """Stop FastAPI server"""
        self.is_running = False
        if self.uvicorn_server:
            self.uvicorn_server.should_exit = True
        if self.video_http_server:
            self.video_http_server.should_exit = True
        if self.server_thread:
            print("[WebUI] Stopping FastAPI server")
            if self.server_thread is not threading.current_thread():
                self.server_thread.join(timeout=10)
                if not self.server_thread.is_alive():
                    self.server_thread = None
        if self.video_http_thread:
            if self.video_http_thread is not threading.current_thread():
                self.video_http_thread.join(timeout=5)
                if not self.video_http_thread.is_alive():
                    self.video_http_thread = None

def create_web_interface(config, character_name):
    """Factory function for WebChatInterface"""
    return WebChatInterface(config, character_name)
