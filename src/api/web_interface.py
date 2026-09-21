#!/usr/bin/env python3
"""
FastAPI WebSocket interface wrapper
Provides compatibility layer for existing VoiceChatMode
"""

import asyncio
import errno
import logging
import math
import os
import socket
import threading
import time
import uvicorn
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from .server import create_web_interface as create_fastapi_interface
from src.utils.startup_timing import get_startup_timer
from src.utils.startup_console import safe_startup_reason, startup_console
from src.utils.logging_config import FILE_ONLY_LOG_EXTRA


_startup_timer = get_startup_timer()
logger = logging.getLogger(__name__)

# Database-backed lifespan reconciliation can legitimately exceed one minute
# on the Windows personal installation.  Keep the default long enough to avoid
# declaring a healthy backend dead at the exact readiness boundary while still
# retaining the bounded environment override below.
_DEFAULT_FASTAPI_STARTUP_TIMEOUT_SECONDS = 120.0
_MAX_FASTAPI_STARTUP_TIMEOUT_SECONDS = 600.0
_STARTUP_CANCEL_JOIN_SECONDS = 2.0


# Keep readiness-clock calls behind tiny helpers so regression tests can model
# a lifespan that is logically longer than ten seconds without sleeping for
# ten wall-clock seconds. Production uses the normal monotonic clock/sleep.
def _startup_monotonic() -> float:
    return time.monotonic()


def _startup_sleep(seconds: float) -> None:
    time.sleep(seconds)


class FastAPIStartupFailureKind(str, Enum):
    """Classification for a FastAPI listener that did not become ready."""

    BIND_UNAVAILABLE = "bind_unavailable"
    THREAD_START_FAILED = "thread_start_failed"
    THREAD_EXITED = "thread_exited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class FastAPIStartupFailure:
    """Bounded, observable startup failure state."""

    kind: FastAPIStartupFailureKind
    message: str


def _fastapi_startup_timeout_seconds() -> float:
    """Return the bounded wait for Uvicorn's ASGI lifespan startup.

    Startup performs database-backed reconciliation before Uvicorn marks the
    server as started.  Ten seconds is too short when that work encounters
    normal database contention, so keep a production-safe default while
    retaining an escape hatch for slower installations.
    """
    raw = os.getenv("AOITALK_FASTAPI_STARTUP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_FASTAPI_STARTUP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FASTAPI_STARTUP_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_FASTAPI_STARTUP_TIMEOUT_SECONDS
    return min(value, _MAX_FASTAPI_STARTUP_TIMEOUT_SECONDS)

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
        self._start_lock = threading.RLock()
        self.server_thread = None
        self.uvicorn_server = None
        self._server_host: str | None = None
        self._server_port: int | None = None
        self._server_protocol: str | None = None
        self.video_http_server = None
        self.video_http_thread = None
        self._server_loop = None  # uvicornスレッドのイベントループ
        self._startup_cancel_event: threading.Event | None = None
        # Keep a structured readiness result alongside the original
        # exception, which remains useful as a RuntimeError chaining cause.
        self.startup_failure: FastAPIStartupFailure | None = None
        self._startup_error: BaseException | None = None

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
            startup_console.warning("Video HTTP server module is unavailable; skipping")
            return
        if not is_loopback_host(host):
            raise ValueError("Video HTTP server must bind to a loopback host")

        startup_cancel_event = getattr(self, "_startup_cancel_event", None)

        def run_video_server():
            loop = None
            try:
                if startup_cancel_event is not None and startup_cancel_event.is_set():
                    return
                logger.info(
                    "Starting HTTP video server on http://%s:%s",
                    host,
                    video_port,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
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
                if startup_cancel_event is not None and startup_cancel_event.is_set():
                    self.video_http_server.should_exit = True
                    return

                async def serve_video():
                    """Capture BaseException inside the task before re-raising."""
                    try:
                        await self.video_http_server.serve()
                    except BaseException as error:
                        return error

                serve_error = loop.run_until_complete(serve_video())
                if serve_error is not None:
                    raise serve_error
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, GeneratorExit)):
                    raise
                reason = safe_startup_reason(error, limit=240)
                logger.error(
                    "Video HTTP server failed: %s",
                    reason,
                    exc_info=True,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
                startup_console.warning(
                    f"Video HTTP server failed on {host}:{video_port}: {reason}; "
                    "see detailed log"
                )
            finally:
                if loop is not None:
                    try:
                        loop.close()
                    except Exception:
                        pass
        
        self.video_http_thread = threading.Thread(target=run_video_server, daemon=True)
        self.video_http_thread.start()

    def _stop_video_http_server(self) -> None:
        """Cancel and join the optional SSL video helper for this generation."""

        startup_cancel_event = getattr(self, "_startup_cancel_event", None)
        if startup_cancel_event is not None:
            startup_cancel_event.set()
        video_server = getattr(self, "video_http_server", None)
        if video_server is not None:
            try:
                video_server.should_exit = True
            except Exception:
                pass
        video_thread = getattr(self, "video_http_thread", None)
        if video_thread is not None and video_thread is not threading.current_thread():
            try:
                video_thread.join(timeout=5)
            except Exception:
                pass
            try:
                if not video_thread.is_alive():
                    self.video_http_thread = None
            except Exception:
                self.video_http_thread = None

    @staticmethod
    def _safe_startup_exception_message(
        error: BaseException,
        *,
        limit: int = 500,
    ) -> str:
        """Return concise exception text for a startup failure."""
        if isinstance(error, SystemExit):
            code = error.code
            if code in (None, 0):
                return "Uvicorn exited during startup"
            return f"Uvicorn exited during startup (code {code})"

        # Dependency exception text can be unexpectedly large and may include
        # connection URLs or key/value secrets. Keep the readiness result
        # bounded and credential-redacted before it reaches the operator
        # console (the complete traceback remains in the app log).
        return safe_startup_reason(error, limit=limit)

    @staticmethod
    def _is_bind_failure(error: BaseException) -> bool:
        """Best-effort detection of a socket bind failure."""
        if isinstance(error, OSError):
            if getattr(error, "errno", None) in {
                errno.EADDRINUSE,
                errno.EADDRNOTAVAIL,
                errno.EACCES,
            }:
                return True

        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "address already in use",
                "only one usage of each socket address",
                "cannot assign requested address",
                "winerror 10048",
                "[errno 98]",
                "[errno 99]",
            )
        )

    def _classify_server_failure(
        self,
        error: BaseException,
        *,
        host: str,
        port: int,
    ) -> FastAPIStartupFailure:
        """Classify an exception raised by the Uvicorn worker thread."""
        # Uvicorn raises SystemExit(3) for bind errors after logging the
        # underlying OSError.  A second probe preserves that distinction when
        # the competing listener is still holding the port; otherwise the same
        # exit status represents an ASGI lifespan/server failure.
        bind_failure = self._is_bind_failure(error)
        if isinstance(error, SystemExit) and not bind_failure:
            try:
                bind_failure = not self._can_bind(host, port)
            except Exception:
                bind_failure = False

        if bind_failure:
            prefix = f"FastAPI could not bind {host}:{port}: "
            reason = self._safe_startup_exception_message(
                error,
                limit=max(1, 500 - len(prefix)),
            )
            return FastAPIStartupFailure(
                FastAPIStartupFailureKind.BIND_UNAVAILABLE,
                f"{prefix}{reason}",
            )
        prefix = f"FastAPI startup failed on {host}:{port}: "
        reason = self._safe_startup_exception_message(
            error,
            limit=max(1, 500 - len(prefix)),
        )
        return FastAPIStartupFailure(
            FastAPIStartupFailureKind.SERVER_ERROR,
            f"{prefix}{reason}",
        )

    def _raise_startup_failure(
        self,
        failure: FastAPIStartupFailure,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Raise genuine failures while retaining historical ``None`` paths."""
        if failure.kind not in {
            FastAPIStartupFailureKind.THREAD_START_FAILED,
            FastAPIStartupFailureKind.SERVER_ERROR,
        }:
            return

        wrapped = RuntimeError(failure.message)
        if error is not None:
            raise wrapped from error
        raise wrapped

    def start_server(self, host='127.0.0.1', port=3000, debug=False,
                     ssl_keyfile=None, ssl_certfile=None):
        """Serialize startup generations and start the FastAPI server."""
        start_lock = getattr(self, "_start_lock", None)
        if start_lock is None:
            # Some focused tests construct the wrapper with ``object.__new__``
            # to avoid the application factory. ``setdefault`` keeps that
            # compatibility path atomic if two callers race on first use.
            start_lock = self.__dict__.setdefault("_start_lock", threading.RLock())
        with start_lock:
            return self._start_server_impl(
                host=host,
                port=port,
                debug=debug,
                ssl_keyfile=ssl_keyfile,
                ssl_certfile=ssl_certfile,
            )

    def _start_server_impl(self, host='127.0.0.1', port=3000, debug=False,
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

        # Do not discard the live generation before checking whether another
        # caller already owns it.  A concurrent/duplicate start must be
        # idempotent once ready and must leave the existing server reference
        # intact so stop_server() can still signal it.
        existing_thread = getattr(self, "server_thread", None)
        if existing_thread is not None and existing_thread.is_alive():
            existing_server = getattr(self, "uvicorn_server", None)
            existing_should_exit = bool(
                existing_server is not None
                and getattr(existing_server, "should_exit", False)
            )
            existing_host = getattr(self, "_server_host", None)
            existing_port = getattr(self, "_server_port", None)
            existing_protocol = getattr(self, "_server_protocol", None)
            if (
                existing_server is not None
                and getattr(existing_server, "started", False)
                and not existing_should_exit
                and existing_host == host
                and existing_port == port
                and existing_protocol == protocol
            ):
                self.is_running = True
                return f"{protocol}://{host}:{port}"
            logger.warning(
                "FastAPI server start is already in progress or shutting down; "
                "keeping the existing worker",
                extra=FILE_ONLY_LOG_EXTRA,
            )
            return None

        # Every invocation owns a fresh readiness generation.  In particular,
        # do not let a stopped server's ``started=True`` flag satisfy a
        # subsequent restart before the new Uvicorn instance is constructed.
        if getattr(self, "video_http_thread", None) is not None:
            self._stop_video_http_server()
        self.startup_failure = None
        self._startup_error = None
        self.uvicorn_server = None
        self._server_loop = None
        self.video_http_server = None
        self.video_http_thread = None
        self._server_host = host
        self._server_port = port
        self._server_protocol = protocol
        startup_cancel_event = threading.Event()
        self._startup_cancel_event = startup_cancel_event

        with _startup_timer.phase("startup.web.fastapi.listener_probe"):
            can_bind = self._can_bind(host, port)
        if not can_bind:
            self.startup_failure = FastAPIStartupFailure(
                FastAPIStartupFailureKind.BIND_UNAVAILABLE,
                f"FastAPI could not bind {host}:{port}: the port is unavailable",
            )
            logger.error(
                "FastAPI port %s is unavailable on %s; the backend was not started",
                port,
                host,
                extra=FILE_ONLY_LOG_EXTRA,
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
            logger.info(
                "HTTP video server disabled by configuration",
                extra=FILE_ONLY_LOG_EXTRA,
            )

        def run_server():
            loop = None
            try:
                logger.info(
                    "Starting FastAPI server on %s://%s:%s",
                    protocol,
                    host,
                    port,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
                if use_ssl:
                    logger.info("FastAPI SSL enabled", extra=FILE_ONLY_LOG_EXTRA)
                # A timeout can win before the worker has finished building
                # its event loop/configuration.  Check the per-start cancel
                # generation before creating a server so a late worker cannot
                # unexpectedly expose a listener after start_server() failed.
                if startup_cancel_event.is_set():
                    return
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
                if startup_cancel_event.is_set():
                    self.uvicorn_server.should_exit = True
                    return
                loop.run_until_complete(self.uvicorn_server.serve())
            except BaseException as e:
                # Uvicorn deliberately raises SystemExit(3) for ASGI lifespan
                # and bind failures.  Catch failures in this worker so the
                # caller receives a structured result rather than an
                # unhandled-thread traceback.  KeyboardInterrupt/GeneratorExit
                # remain thread control-flow exceptions and are not converted
                # into application startup errors.
                if isinstance(e, (KeyboardInterrupt, GeneratorExit)):
                    raise
                self.is_running = False
                self._startup_error = e
                self.startup_failure = self._classify_server_failure(
                    e,
                    host=host,
                    port=port,
                )
                logger.error(
                    self.startup_failure.message,
                    exc_info=True,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            finally:
                self._server_loop = None
                if loop is not None:
                    try:
                        loop.close()
                    except Exception:
                        pass
                
        self.is_running = True
        with _startup_timer.phase("startup.web.fastapi.thread_spawn"):
            try:
                self.server_thread = threading.Thread(target=run_server, daemon=True)
                self.server_thread.start()
            except BaseException as e:
                # This call runs on the process' caller thread.  Preserve
                # normal process control-flow exits while still classifying
                # unusual BaseException failures from a thread implementation.
                if isinstance(e, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise
                self.is_running = False
                self._startup_error = e
                self.startup_failure = FastAPIStartupFailure(
                    FastAPIStartupFailureKind.THREAD_START_FAILED,
                    f"FastAPI server thread could not be started: "
                    f"{self._safe_startup_exception_message(e)}",
                )
                # The optional HTTPS video helper is started before the
                # FastAPI worker.  If the worker thread itself cannot be
                # created, tear that helper down as part of the same failed
                # startup generation rather than leaving a listener behind.
                self._stop_video_http_server()
                self._raise_startup_failure(
                    self.startup_failure,
                    error=e,
                )
                return None

        # Uvicorn が lifespan startup を完了するまで待つ。固定 sleep だけでは、
        # bind 失敗したスレッドを起動成功として Caddy を公開してしまう。
        startup_timeout = _fastapi_startup_timeout_seconds()
        with _startup_timer.phase("startup.web.fastapi.readiness_poll"):
            deadline = _startup_monotonic() + startup_timeout
            while _startup_monotonic() < deadline:
                server = self.uvicorn_server
                thread = self.server_thread
                if (
                    server is not None
                    and getattr(server, "started", False)
                    and thread is not None
                    and thread.is_alive()
                    and not getattr(server, "should_exit", False)
                ):
                    # ``Server.started`` is set after sockets are bound and the
                    # ASGI lifespan startup has completed. Keep listener and
                    # HTTP handling milestones distinct in the log even though
                    # Uvicorn exposes them through this single readiness flag.
                    _startup_timer.mark("startup.web.fastapi.listener_ready")
                    _startup_timer.mark("startup.web.fastapi.lifespan_ready")
                    _startup_timer.mark("startup.web.fastapi.http_ready")
                    return f"{protocol}://{host}:{port}"
                if (
                    server is not None
                    and getattr(server, "started", False)
                    and (
                        thread is None
                        or not thread.is_alive()
                        or getattr(server, "should_exit", False)
                    )
                ):
                    self.startup_failure = FastAPIStartupFailure(
                        FastAPIStartupFailureKind.THREAD_EXITED,
                        "FastAPI server stopped immediately after readiness",
                    )
                    break
                if thread is None or not thread.is_alive():
                    break
                _startup_sleep(0.05)

            self.is_running = False
            # Check once after the deadline to avoid a boundary race where a
            # healthy lifespan completes just as the polling window expires.
            server = self.uvicorn_server
            thread = self.server_thread
            if (
                server is not None
                and getattr(server, "started", False)
                and thread is not None
                and thread.is_alive()
                and not getattr(server, "should_exit", False)
            ):
                _startup_timer.mark("startup.web.fastapi.listener_ready")
                _startup_timer.mark("startup.web.fastapi.lifespan_ready")
                _startup_timer.mark("startup.web.fastapi.http_ready")
                self.is_running = True
                return f"{protocol}://{host}:{port}"

            failure = self.startup_failure
            if failure is None:
                if self._startup_error is not None:
                    failure = self._classify_server_failure(
                        self._startup_error,
                        host=host,
                        port=port,
                    )
                    self.startup_failure = failure
                elif thread is None or not thread.is_alive():
                    failure = FastAPIStartupFailure(
                        FastAPIStartupFailureKind.THREAD_EXITED,
                        "FastAPI server thread exited before readiness",
                    )
                    self.startup_failure = failure
                else:
                    failure = FastAPIStartupFailure(
                        FastAPIStartupFailureKind.TIMEOUT,
                        f"FastAPI server did not become ready on "
                        f"{protocol}://{host}:{port} within {startup_timeout:g}s",
                    )
                    self.startup_failure = failure

            if failure.kind in {
                FastAPIStartupFailureKind.THREAD_START_FAILED,
                FastAPIStartupFailureKind.SERVER_ERROR,
            }:
                self._stop_video_http_server()
                self._raise_startup_failure(
                    failure,
                    error=self._startup_error,
                )

            if failure.kind is FastAPIStartupFailureKind.TIMEOUT:
                # Signal cancellation even when Uvicorn has not been
                # constructed yet.  The worker checks this event before and
                # after construction, preventing a delayed startup path from
                # leaking a hidden backend listener.
                startup_cancel_event.set()
                if server is not None:
                    server.should_exit = True
                thread = self.server_thread
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=_STARTUP_CANCEL_JOIN_SECONDS)
                    if thread.is_alive():
                        logger.warning(
                            "FastAPI startup worker is still shutting down after timeout",
                            extra=FILE_ONLY_LOG_EXTRA,
                        )
                self._stop_video_http_server()
                logger.error(failure.message, extra=FILE_ONLY_LOG_EXTRA)
            elif failure.kind in {
                FastAPIStartupFailureKind.THREAD_EXITED,
                FastAPIStartupFailureKind.BIND_UNAVAILABLE,
            }:
                self._stop_video_http_server()
                logger.error(failure.message, extra=FILE_ONLY_LOG_EXTRA)
            return None
        
    def stop_server(self):
        """Stop FastAPI server"""
        self.is_running = False
        startup_cancel_event = getattr(self, "_startup_cancel_event", None)
        if startup_cancel_event is not None:
            startup_cancel_event.set()
        if self.uvicorn_server:
            self.uvicorn_server.should_exit = True
        self._stop_video_http_server()
        if self.server_thread:
            logger.info("Stopping FastAPI server", extra=FILE_ONLY_LOG_EXTRA)
            if self.server_thread is not threading.current_thread():
                self.server_thread.join(timeout=10)
                if not self.server_thread.is_alive():
                    self.server_thread = None

def create_web_interface(config, character_name):
    """Factory function for WebChatInterface"""
    return WebChatInterface(config, character_name)
