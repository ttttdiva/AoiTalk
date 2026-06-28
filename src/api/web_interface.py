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

class WebChatInterface:
    """Wrapper class for FastAPI WebSocket server"""
    
    def __init__(self, config, character_name):
        """Initialize FastAPI wrapper"""
        self.config = config
        self.character_name = character_name
        self.server = create_fastapi_interface(config, character_name)
        self.app = self.server.get_app()
        
        # Server state
        self.is_running = False
        self.server_thread = None
        self.uvicorn_server = None
        self.video_http_server = None
        self.video_http_thread = None
        self._server_loop = None  # uvicornスレッドのイベントループ

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
            # デフォルトはTrue（後方互換性のため）
            return video_config.get('enabled', True)
        except Exception:
            return True

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
    
    def _start_video_http_server(self, host: str, video_port: int):
        """Start HTTP video server in a separate thread"""
        try:
            from .video_http_server import create_video_http_app
        except ImportError:
            print("[WebUI] ⚠️ Video HTTP server module not found, skipping")
            return
        
        def run_video_server():
            try:
                print(f"[WebUI] 🎬 Starting HTTP video server on http://{host}:{video_port}")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                video_app = create_video_http_app()
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

        if not self._can_bind(host, port):
            print(
                f"[WebUI] Port {port} is already in use on {host}; "
                "FastAPI server was not started."
            )
            self.is_running = False
            return None

        # Start HTTP video server if using SSL AND enabled in config (for Android compatibility)
        if use_ssl and self._is_video_http_enabled():
            video_port = self._get_video_http_port(port)
            self._start_video_http_server(host, video_port)
        elif use_ssl and not self._is_video_http_enabled():
            print("[WebUI] ℹ️ HTTP video server disabled in config")
        
        def run_server():
            try:
                print(f"[WebUI] Starting FastAPI server on {protocol}://{host}:{port}")
                if use_ssl:
                    print(f"[WebUI] 🔐 SSL enabled - using HTTPS")
                # Create new event loop for the thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._server_loop = loop  # broadcast用に保存
                
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
                loop.run_until_complete(self.uvicorn_server.serve())
            except Exception as e:
                print(f"[WebUI] Server error: {e}")
                
        self.is_running = True
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        
        # Wait for server to start
        import time
        time.sleep(2)
        
        return f"{protocol}://{host}:{port}"
        
    def stop_server(self):
        """Stop FastAPI server"""
        self.is_running = False
        if self.uvicorn_server:
            self.uvicorn_server.should_exit = True
        if self.video_http_server:
            self.video_http_server.should_exit = True
        if self.server_thread:
            print("[WebUI] Stopping FastAPI server")

def create_web_interface(config, character_name):
    """Factory function for WebChatInterface"""
    return WebChatInterface(config, character_name)
