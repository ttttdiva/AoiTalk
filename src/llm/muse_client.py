"""AoiTalk Chat client for Muse (``provider=muse-remote``, ``model=muse-chat``).

Natural-language Chat only: the owner's text goes to Muse unchanged and Muse's
natural reply comes back as the ordinary assistant message.  No AoiTalk tool
calling and no JSON contract are involved; the strict scoped tool loop is a
separate development opt-in.  Delivery, idempotency and late-reply recovery
live in :mod:`src.services.muse_chat_service`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import threading
import uuid
from typing import Any, Awaitable, Callable, Dict, Generator, Optional

from ..memory.history import HistoryManager
from ..services.muse_chat_service import (
    MODEL_ID,
    PROVIDER_ID,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_PENDING,
    STATE_REPLIED,
    STATE_UNCERTAIN,
    CLISideSessionPort,
    MuseChatError,
    MuseChatService,
    MuseChatSettings,
    MuseChatUnavailable,
    require_muse_chat_settings,
)
from .provider_capabilities import ProviderCapabilities

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[list[str]]]
ServiceFactory = Callable[[MuseChatSettings], Any]

MUSE_WAITING_TEXT = (
    "【Muse作業中】Museに作業を送信しました。返信が届き次第、この会話に自動で表示されます。"
)

_STATE_ERRORS = {
    STATE_UNCERTAIN: (
        "REPLY_UNCERTAIN",
        "Museへの送信結果を確認できませんでした。二重送信を避けるため再送していません。",
    ),
    STATE_FAILED: ("SEND_FAILED", "Museの処理が失敗しました。"),
    STATE_CANCELLED: (
        "CANCELLED_LOCAL",
        "AoiTalk側で待機を取り消しました。Muse側の処理は取り消していません。",
    ),
}

_MUSE_CHAT_TURN_NAMESPACE = uuid.UUID("fd44f604-614f-5ea0-8af0-1958018e55c4")


class MuseChatClient:
    """Chat-only provider client for ``provider=muse-remote``/``muse-chat``."""

    provider_label = PROVIDER_ID
    model_name = MODEL_ID

    def __init__(self, config: Any, *, service_factory: ServiceFactory | None = None):
        self.config = config
        # Raises MuseChatUnavailable (ValueError) when not configured.
        self.settings = require_muse_chat_settings(config)
        self._service_factory: ServiceFactory = service_factory or self._default_service_factory
        self.capabilities = ProviderCapabilities(
            supports_stream=False,
            supports_tools=False,
            supports_response_format=False,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=False,
        )
        self.character_name = "Muse"
        self.history_manager = HistoryManager()
        self.memory_manager = None
        self.current_session_id: str | None = None
        self.current_project_id: str | None = None
        self.current_edit_message_id: str | None = None
        self.current_assistant_message_id: str | None = None
        self.current_response_model: Any = None
        self.current_include_project_context: bool | None = None
        self.current_command_capabilities: tuple[str, ...] = ()
        self.current_tool_required: bool | None = None
        self.external_persistence_enabled = False
        self.session_user_id = "default_user"
        self.session_metadata: dict[str, Any] = {}
        self.generation_policy: Any = None
        self.system_prompt = ""
        self._last_generation_metadata: dict[str, Any] = {}
        self._provider_state = {"previous_response_id": None, "fingerprint": None}
        self._provider_state_mode = "stateless"

    def _default_service_factory(self, _snapshot: MuseChatSettings) -> MuseChatService:
        settings = require_muse_chat_settings(self.config)
        if settings.native_link:
            from ..services.muse_native_port import NativeMuseSidePort

            port = NativeMuseSidePort(self.config)
        else:
            port = CLISideSessionPort(self.config)
        return MuseChatService(settings, port)

    async def _close_service_port(self, service: Any) -> None:
        port = getattr(service, "port", None)
        close = getattr(port, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # Closing a native socket is best-effort and must not replace the
            # Chat result or expose provider details.
            pass

    # -- compatibility surface ----------------------------------------------

    def set_session_context(self, user_id: str = "default_user", metadata: Any = None) -> None:
        self.session_user_id = user_id
        self.session_metadata = dict(metadata or {})

    def set_system_prompt(self, prompt: str) -> None:
        # Muse keeps its own persona remotely; AoiTalk prompts are not sent.
        self.system_prompt = str(prompt or "")

    def set_isolated_system_prompt(self, prompt: str) -> None:
        self.system_prompt = str(prompt or "")

    def set_character(self, character_name: str) -> None:
        self.character_name = character_name

    def update_character(self, character_name: str) -> None:
        self.set_character(character_name)

    def clear_history(self) -> None:
        self.history_manager.clear()

    def set_llm_mode(self, mode: str) -> None:
        return None

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": MODEL_ID, "label": "Muse"}]

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_label, "model": self.model_name}

    def get_generation_metadata(self) -> dict[str, Any]:
        return dict(self._last_generation_metadata)

    async def cleanup(self) -> None:
        return None

    # -- generation -----------------------------------------------------------

    def _turn_identity(self) -> tuple[str, str, str]:
        owner_id = str(self.session_user_id or "").strip()
        chat_id = str(self.current_session_id or "").strip()
        if not owner_id or not chat_id or owner_id == "default_user":
            raise MuseChatUnavailable(
                "CHAT_SESSION_REQUIRED",
                "Muse ChatはログインユーザーのChatセッション内でのみ送信できます。",
            )
        # Session clients are ephemeral, but session_llm_generation projects
        # their state back onto the long-lived client. That makes
        # current_assistant_message_id stale on the next user turn. Bind
        # Muse's idempotency key to the canonical persisted user message:
        # retries of one turn keep the same key while the next turn gets a
        # different key and assistant row identity.
        try:
            from ..services.turn_context import get_turn_context

            user_message_id = str(get_turn_context().message_id or "").strip()
        except Exception:
            user_message_id = ""
        if user_message_id:
            turn_key = str(
                uuid.uuid5(
                    _MUSE_CHAT_TURN_NAMESPACE,
                    f"{chat_id}:{user_message_id}",
                )
            )
            self.current_assistant_message_id = turn_key
            return owner_id, chat_id, turn_key

        turn_key = str(self.current_assistant_message_id or "").strip()
        if not turn_key:
            turn_key = str(uuid.uuid4())
            self.current_assistant_message_id = turn_key
        return owner_id, chat_id, turn_key

    def finalize_turn_identity(self) -> None:
        """Release the assistant row identity after this Chat turn is saved."""

        # Retries within one turn run before terminal-mode cleanup and keep the
        # same persisted user/message identity.  Once the waiting marker or a
        # real reply has been saved, the next user turn must allocate a fresh
        # key so a late reply cannot attach to the previous placeholder.
        self.current_assistant_message_id = None

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
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
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        if image_data:
            raise MuseChatUnavailable("IMAGE_UNSUPPORTED", "Muse Chatは画像入力に対応していません。")
        owner_id, chat_id, turn_key = self._turn_identity()
        service = self._service_factory(self.settings)
        cancel_event = threading.Event()
        try:
            result = await service.send_turn(
                owner_id,
                chat_id,
                turn_key,
                str(user_input or ""),
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            # Local stop only; the admitted run is recovered from history.
            cancel_event.set()
            raise
        finally:
            await self._close_service_port(service)
        self._last_generation_metadata = {
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "muse_chat_run_id": result.run_id,
            # ``pending`` is the transport state.  ``waiting`` is the
            # user-facing Chat state and is deliberately stable for the
            # persisted waiting placeholder.
            "muse_chat_state": (
                "waiting" if result.state == STATE_PENDING else result.state
            ),
            "muse_chat_side_session_id": result.side_session_id,
            "muse_chat_turn_key": turn_key,
            "muse_chat": {
                "run_id": result.run_id,
                "state": "waiting" if result.state == STATE_PENDING else result.state,
                "side_session_id": result.side_session_id,
                "turn_key": turn_key,
                "waiting": result.state == STATE_PENDING,
            },
        }
        if result.state == STATE_REPLIED and isinstance(result.text, str) and result.text:
            return result.text
        if result.state == STATE_PENDING:
            # A pending receipt is a successful hand-off.  Returning a stable
            # display marker lets the normal Chat persistence path create the
            # assistant row under ``turn_key``; the recovery worker later
            # appends the real reply without replacing a newer turn.
            return MUSE_WAITING_TEXT
        code, message = _STATE_ERRORS.get(result.state, ("REPLY_UNAVAILABLE", "Museの返信を取得できませんでした。"))
        raise MuseChatError(code, message, run_id=result.run_id)

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> str | Generator[str, None, None]:
        execution_context = contextvars.copy_context()

        def run() -> str:
            return asyncio.run(
                self.generate_response_async(
                    user_input,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    image_data=image_data,
                    stream_callback=stream_callback,
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(execution_context.run, run).result()
        if stream:
            return iter((result,))
        return result

    def generate(self, prompt: str) -> str:
        return str(self.generate_response(prompt, stream=False))

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        prompt = "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in messages)
        return self.generate(prompt)


def create_muse_chat_client(config: Any) -> MuseChatClient:
    return MuseChatClient(config)
