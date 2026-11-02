"""AoiTalk Chat client that talks to Grok Bot through native integration.

The client never calls a model API.  Each Chat turn is delegated to
:class:`~src.services.grok_bot_chat_service.GrokBotChatService`, which
persists the receipt, sends once per ``(owner, chat, generation, turn)`` and
reconciles replies read-only.  Grok Bot runs its own tools remotely, so this
client exposes no AoiTalk tool calling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import uuid
from typing import Any, Awaitable, Callable, Dict, Generator, Optional

from ..memory.history import HistoryManager
from ..services.grok_bot_chat_service import (
    MODEL_ID,
    PROVIDER_ID,
    REMOTE_CANCEL,
    REMOTE_TERMINAL,
    STATE_PENDING,
    STATE_EMPTY_RESPONSE,
    STATE_INCOMPLETE_RESPONSE,
    STATE_REPLIED,
    STATE_REPLY_OBSERVED,
    STATE_SENT,
    STATE_UNCERTAIN,
    GrokBotChatError,
    GrokBotChatService,
    GrokBotChatSettings,
    GrokBotChatUnavailable,
    build_operation_id,
    require_grok_bot_chat_settings,
)
from .provider_capabilities import ProviderCapabilities

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[list[str]]]
ServiceFactory = Callable[[GrokBotChatSettings], Any]

GROK_WAITING_TEXT = (
    "【Grok作業中】Grok Botに作業を送信しました。返信が届き次第、この会話に自動で表示されます。"
)

_STATE_ERRORS = {
    STATE_REPLY_OBSERVED: (
        "REMOTE_TERMINAL_UNSUPPORTED",
        "Grok Botの返信断片は確認できましたが、完了状態を確認できません。"
        "最終応答とは扱っていません。履歴を再読み込みすると読み取り専用で再照合します。",
    ),
    STATE_EMPTY_RESPONSE: (
        "EMPTY_FINAL_RESPONSE",
        "Grok Bot側の処理は終了しましたが、最終テキストが空でした。成功とは扱っていません。",
    ),
    STATE_INCOMPLETE_RESPONSE: (
        "RESPONSE_TRUNCATED",
        "Grok Botの最終応答が途中で切れています。完了とは扱っていません。",
    ),
    "rejected": ("SEND_REJECTED", "Grok Botが送信を受け付けませんでした。"),
    "failed": ("SEND_FAILED", "Grok Botへ送信できませんでした。"),
    "misrouted": (
        "MISROUTED",
        "Grok Botの送信先または応答の対応付けが一致しないため、応答を表示しません。",
    ),
    "cancelled_local": (
        "CANCELLED_LOCAL",
        "AoiTalk側で待機を取り消しました。Grok Bot側の処理は取り消していません。",
    ),
}
_GROK_CHAT_TURN_NAMESPACE = uuid.UUID("a1b9c7e2-3f4d-5a60-8b71-2c9d0e4f6a85")

class GrokBotChatClient:
    """Chat-only provider client for ``provider=grok-bot``."""

    provider_label = PROVIDER_ID
    model_name = MODEL_ID

    def __init__(self, config: Any, *, service_factory: ServiceFactory | None = None):
        self.config = config
        # Raises GrokBotChatUnavailable when the explicit binding is missing.
        self.settings = require_grok_bot_chat_settings(config)
        # The default factory re-reads the binding at send time, so a binding
        # generation/target change after this client was built is refused as
        # stale instead of being sent under the old snapshot.
        self._service_factory: ServiceFactory = service_factory or (
            lambda _snapshot: GrokBotChatService(require_grok_bot_chat_settings(self.config))
        )
        self._background_close_tasks: set[asyncio.Task[Any]] = set()
        self.capabilities = ProviderCapabilities(
            supports_stream=False,
            supports_tools=False,
            supports_response_format=False,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=False,
        )
        self.character_name = "Grok Bot"
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

    # -- compatibility surface ----------------------------------------------

    def set_session_context(self, user_id: str = "default_user", metadata: Any = None) -> None:
        self.session_user_id = user_id
        self.session_metadata = dict(metadata or {})

    def set_system_prompt(self, prompt: str) -> None:
        # Grok Bot keeps its own persona/system prompt remotely.
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
        return [{"id": MODEL_ID, "label": "Grok Bot"}]

    def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_label,
            "model": self.model_name,
            "remote_terminal": REMOTE_TERMINAL,
            "remote_terminal_supported": False,
            "remote_cancel": REMOTE_CANCEL,
            "remote_cancel_supported": False,
        }

    def get_generation_metadata(self) -> dict[str, Any]:
        return dict(self._last_generation_metadata)

    async def cleanup(self) -> None:
        tasks = tuple(self._background_close_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _close_service(service: Any) -> None:
        close = getattr(service, "close", None)
        if not callable(close):
            return
        try:
            await asyncio.to_thread(close)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Transport close is best-effort and must not replace the
            # Chat result or leak provider details.
            pass

    @classmethod
    async def _close_after_worker(cls, service: Any, worker: asyncio.Task[Any]) -> None:
        try:
            await asyncio.shield(worker)
        except Exception:
            pass
        await cls._close_service(service)

    def _defer_close_after_worker(
        self, service: Any, worker: asyncio.Task[Any]
    ) -> None:
        task = asyncio.create_task(self._close_after_worker(service, worker))
        self._background_close_tasks.add(task)
        task.add_done_callback(self._background_close_tasks.discard)

    # -- generation -----------------------------------------------------------

    def _turn_identity(self) -> tuple[str, str, str]:
        owner_id = str(self.session_user_id or "").strip()
        chat_id = str(self.current_session_id or "").strip()
        if not owner_id or not chat_id:
            raise GrokBotChatUnavailable(
                "CHAT_SESSION_REQUIRED",
                "Grok Bot ChatはログインユーザーのChatセッション内でのみ送信できます。",
            )
        # Session clients are ephemeral, but session_llm_generation projects
        # their state back onto the long-lived client, so a stored key can be
        # stale on the next user turn and collide with the previous turn's
        # assistant row.  Bind the key to the canonical persisted user message:
        # retries of one turn keep the same key, the next turn gets a new one.
        try:
            from ..services.turn_context import get_turn_context

            user_message_id = str(get_turn_context().message_id or "").strip()
        except Exception:
            user_message_id = ""
        if user_message_id:
            turn_key = str(
                uuid.uuid5(_GROK_CHAT_TURN_NAMESPACE, f"{chat_id}:{user_message_id}")
            )
            self.current_assistant_message_id = turn_key
            return owner_id, chat_id, turn_key

        turn_key = str(self.current_assistant_message_id or "").strip()
        if not turn_key:
            # Chat message ids are UUIDs; the turn key doubles as the assistant
            # message id that history recovery links late replies to.
            turn_key = str(uuid.uuid4())
            self.current_assistant_message_id = turn_key
        return owner_id, chat_id, turn_key

    def finalize_turn_identity(self) -> None:
        """Release the assistant message key after the owning Chat turn ends.

        The terminal mode calls this only after the response (including a
        failure placeholder) has been persisted. A later user turn then gets a
        fresh key, while retries during the current turn keep the same key.
        """

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
            raise GrokBotChatUnavailable(
                "IMAGE_UNSUPPORTED", "Grok Bot Chatは画像入力に対応していません。"
            )
        owner_id, chat_id, turn_key = self._turn_identity()
        binding = self.settings.binding_for(owner_id)
        generation = binding.generation if binding is not None else None
        service = self._service_factory(self.settings)
        cancel_event = threading.Event()
        send_started = threading.Event()
        cancelled_before_start = threading.Event()

        def invoke_send():
            send_started.set()
            if cancelled_before_start.is_set() or cancel_event.is_set():
                return None
            return service.send_turn(
                owner_id,
                chat_id,
                turn_key,
                str(user_input or ""),
                expected_generation=generation,
                cancel_event=cancel_event,
            )

        worker = asyncio.create_task(
            asyncio.to_thread(invoke_send)
        )
        close_deferred = False
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            if send_started.is_set():
                close_deferred = True
                self._defer_close_after_worker(service, worker)
            else:
                # Preserve the existing no-admission guarantee when cancel
                # wins while the default executor has not started the send.
                cancelled_before_start.set()
                worker.cancel()
            if generation is not None:
                operation_id = build_operation_id(owner_id, chat_id, generation, turn_key)
                try:
                    service.cancel_local(owner_id, chat_id, operation_id)
                except (GrokBotChatError, GrokBotChatUnavailable):
                    pass
            raise
        finally:
            if not close_deferred:
                await self._close_service(service)
        self._last_generation_metadata = {
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "grok_bot_operation_id": result.operation_id,
            # Transport states remain in the nested receipt metadata.  The
            # user-facing Chat state is a successful hand-off while the
            # admitted operation is still pending/sent/uncertain.
            "grok_bot_state": (
                "waiting"
                if result.state in {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN}
                else result.state
            ),
            "grok_bot_transport_state": result.state,
            "grok_bot_completion_status": result.completion_status,
            "grok_bot_correlation": dict(result.correlation or {}),
            "remote_terminal": result.remote_terminal,
            "remote_terminal_supported": result.remote_terminal_supported,
            "remote_cancel": REMOTE_CANCEL,
            "remote_cancel_supported": False,
            "response_truncated": bool(result.response_truncated),
            "transport_diagnostics": dict(result.transport_diagnostics or {}),
            "grok_bot": {
                "operation_id": result.operation_id,
                "state": (
                    "waiting"
                    if result.state in {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN}
                    else result.state
                ),
                "transport_state": result.state,
                "waiting": result.state in {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN},
                "completion_status": result.completion_status,
                "correlation": dict(result.correlation or {}),
                "remote_terminal": result.remote_terminal,
                "remote_terminal_supported": result.remote_terminal_supported,
                "remote_cancel": REMOTE_CANCEL,
                "remote_cancel_supported": False,
                "response_truncated": bool(result.response_truncated),
                "transport_diagnostics": dict(result.transport_diagnostics or {}),
            },
        }
        if result.state == STATE_REPLIED:
            return result.text
        if result.state == STATE_REPLY_OBSERVED and result.text.strip():
            # Expose correlated progress text as a Chat response while keeping
            # the receipt explicitly nonterminal.  The metadata above carries
            # the unsupported remote terminal state; this is not proof that
            # Grok Bot finished its processing.
            return result.text
        if result.state in {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN}:
            # Admission succeeded or may have succeeded, but no terminal
            # reply is available in this request.  Return a stable assistant
            # body so ordinary Chat persistence treats this as waiting rather
            # than as an error.  History recovery later replaces this waiting
            # branch with the correlated reply when it arrives.
            return GROK_WAITING_TEXT
        code, message = _STATE_ERRORS.get(
            result.state, ("REPLY_UNAVAILABLE", "Grok Botの返信を取得できませんでした。")
        )
        raise GrokBotChatError(code, message, operation_id=result.operation_id)

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
        prompt = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}" for item in messages
        )
        return self.generate(prompt)


def create_grok_bot_chat_client(config: Any) -> GrokBotChatClient:
    return GrokBotChatClient(config)
