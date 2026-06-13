import inspect

import pytest

from src.assistant.chat_turn_persistence import ChatTurnPersistence
from src.assistant.modes.terminal_mode import TerminalMode
from src.assistant.modes.voice_chat_mode import VoiceChatMode
from src.memory.repository import ConversationRepository


class FakeMessage:
    def __init__(self, id, role, content):
        self.id = id
        self.role = role
        self.content = content
        self.sender_type = None
        self.sender_id = None
        self.sender_display_name = None


class FakeRepository:
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else [
            FakeMessage("m1", "user", "おはよう"),
            FakeMessage("m2", "assistant", "おはようございます"),
            FakeMessage("m3", "user", "今何時?"),
        ]
        self.saved = []

    async def add_message(
        self,
        session_id,
        role,
        content,
        metadata=None,
        branch_from_message_id=None,
        sender_type=None,
        sender_id=None,
        sender_display_name=None,
    ):
        msg = FakeMessage(f"saved-{len(self.saved)}", role, content)
        msg.session_id = session_id
        msg.metadata = metadata or {}
        msg.branch_from_message_id = branch_from_message_id
        msg.sender_type = sender_type
        msg.sender_id = sender_id
        msg.sender_display_name = sender_display_name
        self.saved.append(msg)
        self.messages.append(msg)
        return msg

    async def get_active_branch_messages(self, session_id):
        return list(self.messages)


class FakeMemoryManager:
    def __init__(self, messages=None):
        self.repository = FakeRepository(messages=messages)
        self.initialized = False

    def is_initialized(self):
        return self.initialized

    async def initialize(self):
        self.initialized = True
        return True

    async def add_message_to_session(
        self,
        session_id,
        role,
        content,
        metadata=None,
        success=True,
        branch_from_message_id=None,
        sender_type=None,
        sender_id=None,
        sender_display_name=None,
    ):
        return await self.repository.add_message(
            session_id,
            role,
            content,
            metadata=metadata,
            branch_from_message_id=branch_from_message_id,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
        )


class FakeHistoryManager:
    def __init__(self):
        self.messages = []

    def clear(self):
        self.messages.clear()

    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})


class FakeClient:
    def __init__(self):
        self.history_manager = FakeHistoryManager()
        self.conversation_history = []
        self._loaded_history_session_id = None
        self._loaded_session_id = None


def test_memory_repository_accepts_sender_metadata_contract():
    signature = inspect.signature(ConversationRepository.add_message)

    assert "sender_type" in signature.parameters
    assert "sender_id" in signature.parameters
    assert "sender_display_name" in signature.parameters


@pytest.mark.asyncio
async def test_save_user_then_load_prompt_history_excludes_current_message():
    manager = FakeMemoryManager()
    persistence = ChatTurnPersistence(manager)

    user_message = await persistence.save_user_message(
        session_id="session-1",
        content="さっき俺は何て言った?",
        metadata={"source": "web"},
        branch_from_message_id="m3",
    )

    history = await persistence.load_prompt_history(
        session_id="session-1",
        exclude_message_id=str(user_message.id),
    )

    assert manager.initialized is True
    assert user_message.branch_from_message_id == "m3"
    assert [m["content"] for m in history] == [
        "おはよう",
        "おはようございます",
        "今何時?",
    ]


def test_apply_prompt_history_primes_provider_local_history():
    persistence = ChatTurnPersistence(FakeMemoryManager())
    client = FakeClient()

    persistence.apply_prompt_history_to_client(
        client,
        session_id="session-1",
        prompt_history=[
            {"role": "system", "content": "system note"},
            {"role": "user", "content": "おはよう"},
            {"role": "assistant", "content": "おはようございます"},
        ],
    )

    assert client.history_manager.messages == [
        {"role": "system", "content": "system note"},
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
    ]
    assert client.conversation_history == [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
    ]
    assert client._loaded_history_session_id == "session-1"
    assert client._loaded_session_id == "session-1"


class FakeLLMClient(FakeClient):
    def __init__(self, memory_manager):
        super().__init__()
        self.memory_manager = memory_manager
        self.current_session_id = None
        self.external_persistence_enabled = False
        self.generation_policy = None


class FakeResponseHandler:
    def __init__(self, llm_client):
        self.llm_client = llm_client
        self.tts_manager = None
        self.player = None
        self.seen_history = None
        self.seen_external_persistence = None
        self.seen_generation_profile = None
        self.seen_message = None
        self.seen_stream_callback = None

    def _generate_task_id(self):
        return "task-1"

    async def _generate_response_only(
        self,
        task_id,
        message,
        source,
        image_data=None,
        stream_callback=None,
    ):
        self.seen_stream_callback = stream_callback
        self.seen_message = message
        self.seen_history = list(self.llm_client.history_manager.messages)
        self.seen_external_persistence = self.llm_client.external_persistence_enabled
        self.seen_generation_profile = self.llm_client.generation_policy.profile.value
        return "おはよう、今何時? と言っていました。"


class FakeStreamingLLMClient(FakeLLMClient):
    async def _run_streamed_with_callback(self):
        return None


class FakeFailingResponseHandler(FakeResponseHandler):
    async def _generate_response_only(
        self,
        task_id,
        message,
        source,
        image_data=None,
        stream_callback=None,
    ):
        self.seen_generation_profile = self.llm_client.generation_policy.profile.value
        raise RuntimeError("generation failed")


class FakeStreamingResponseHandler(FakeResponseHandler):
    async def _generate_response_only(
        self,
        task_id,
        message,
        source,
        image_data=None,
        stream_callback=None,
    ):
        response = await super()._generate_response_only(
            task_id,
            message,
            source,
            image_data=image_data,
            stream_callback=stream_callback,
        )
        if stream_callback:
            await stream_callback("stream_start", {"message": "応答を生成しています"})
            await stream_callback("tool_start", {"tool": "web_search"})
            await stream_callback("tool_end", {})
            await stream_callback("stream_end", {"content": response})
        return response


class FakeWebInterface:
    def __init__(self):
        self.assistant_messages = []
        self.events = []

    def add_assistant_message(self, message, session_id=None):
        self.assistant_messages.append({"message": message, "session_id": session_id})

    async def broadcast_stream_event(self, event_type, data):
        self.events.append({"type": event_type, **data})


@pytest.mark.asyncio
async def test_web_chat_turn_saves_and_primes_history_before_llm():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = VoiceChatMode.__new__(VoiceChatMode)
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.tts_manager = None
    mode.player = None
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "さっき俺は何て言った?",
        session_id="session-1",
    )

    assert response_handler.seen_history == [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
        {"role": "user", "content": "今何時?"},
    ]
    assert response_handler.seen_external_persistence is True
    assert [m.role for m in manager.repository.saved] == ["user", "assistant"]
    assert manager.repository.saved[0].content == "さっき俺は何て言った?"
    assert manager.repository.saved[1].content == "おはよう、今何時? と言っていました。"
    assert web_interface.assistant_messages == [
        {"message": "おはよう、今何時? と言っていました。", "session_id": "session-1"}
    ]
    assert [event["type"] for event in web_interface.events] == [
        "conversation_persisted",
        "conversation_persisted",
    ]


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_saves_and_primes_history_before_llm():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "さっき俺は何て言った?",
        session_id="session-1",
    )

    assert response_handler.seen_history == [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
        {"role": "user", "content": "今何時?"},
    ]
    assert response_handler.seen_external_persistence is True
    assert [m.role for m in manager.repository.saved] == ["user", "assistant"]
    assert manager.repository.saved[0].content == "さっき俺は何て言った?"
    assert manager.repository.saved[1].content == "おはよう、今何時? と言っていました。"
    assert web_interface.assistant_messages == [
        {"message": "おはよう、今何時? と言っていました。", "session_id": "session-1"}
    ]
    assert [event["type"] for event in web_interface.events] == [
        "conversation_persisted",
        "conversation_persisted",
    ]


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_applies_explicit_generation_profile():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "普通に返して",
        session_id="session-1",
        generation_profile="autonomous_work",
    )

    assert response_handler.seen_generation_profile == "autonomous_work"


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_persists_assistant_error_when_generation_fails():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeFailingResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "おい",
        session_id="session-1",
        skip_user_persistence=True,
        persisted_user_message_id="saved-current",
    )

    assert [m.role for m in manager.repository.saved] == ["assistant"]
    assert "応答生成中にエラーが発生しました" in manager.repository.saved[0].content
    assert web_interface.assistant_messages[-1]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_broadcasts_stream_progress_events():
    manager = FakeMemoryManager()
    llm_client = FakeStreamingLLMClient(manager)
    response_handler = FakeStreamingResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "検索して",
        session_id="session-1",
    )

    assert response_handler.seen_stream_callback is not None
    assert web_interface.assistant_messages == []
    assert [
        event["type"]
        for event in web_interface.events
        if event["type"] != "conversation_persisted"
    ] == ["stream_start", "tool_start", "tool_end", "stream_end"]
    assert all(
        event["session_id"] == "session-1"
        for event in web_interface.events
        if event["type"] != "conversation_persisted"
    )


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_uses_response_handler_client_after_llm_switch():
    old_manager = FakeMemoryManager(messages=[])
    new_manager = FakeMemoryManager()
    old_client = FakeLLMClient(old_manager)
    new_client = FakeLLMClient(new_manager)
    response_handler = FakeResponseHandler(new_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = old_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "さっき俺は何て言った?",
        session_id="session-1",
    )

    assert response_handler.seen_history == [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
        {"role": "user", "content": "今何時?"},
    ]
    assert [m.role for m in new_manager.repository.saved] == ["user", "assistant"]
    assert old_manager.repository.saved == []


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_can_skip_user_persistence_for_shared_agent_reply():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "蜈ｱ譛峨げ繝ｫ繝ｼ繝励・逕ｨ莉ｶ",
        session_id="session-1",
        skip_user_persistence=True,
        assistant_sender_type="agent",
        assistant_sender_id="confirm",
        assistant_sender_display_name="confirm",
    )

    assert [m.role for m in manager.repository.saved] == ["assistant"]
    assert response_handler.seen_external_persistence is True
    saved_assistant = manager.repository.saved[0]
    assert saved_assistant.sender_type == "agent"
    assert saved_assistant.sender_id == "confirm"
    assert saved_assistant.sender_display_name == "confirm"


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_skips_already_persisted_user_message_in_prompt_history():
    current_user_message = FakeMessage(
        "saved-current",
        "user",
        "さっき俺は何て言った?",
    )
    manager = FakeMemoryManager(
        messages=[
            FakeMessage("m1", "user", "おはよう"),
            FakeMessage("m2", "assistant", "おはようございます"),
            FakeMessage("m3", "user", "今何時?"),
            current_user_message,
        ]
    )
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "さっき俺は何て言った?",
        session_id="session-1",
        skip_user_persistence=True,
        persisted_user_message_id="saved-current",
    )

    assert response_handler.seen_history == [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはようございます"},
        {"role": "user", "content": "今何時?"},
    ]
    assert response_handler.seen_external_persistence is True
    assert [m.role for m in manager.repository.saved] == ["assistant"]


@pytest.mark.asyncio
async def test_voice_web_chat_turn_saves_attachment_metadata_without_mutating_content():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = VoiceChatMode.__new__(VoiceChatMode)
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.tts_manager = None
    mode.player = None
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "この資料見て",
        session_id="session-1",
        client_message_id="client-1",
        attachments=[
            {
                "name": "memo.txt",
                "path": "_projects/project_1/attachments/memo.txt",
                "size": 12,
                "mime_type": "text/plain",
            }
        ],
        attachment_context="[添付ファイル: memo.txt]\n```\nhello\n```",
    )

    saved_user = manager.repository.saved[0]
    assert saved_user.content == "この資料見て"
    assert saved_user.metadata["client_message_id"] == "client-1"
    assert saved_user.metadata["attachments"] == [
        {
            "name": "memo.txt",
            "path": "_projects/project_1/attachments/memo.txt",
            "size": 12,
            "mime_type": "text/plain",
        }
    ]
    assert response_handler.seen_message == (
        "この資料見て\n\n[添付ファイル: memo.txt]\n```\nhello\n```"
    )


@pytest.mark.asyncio
async def test_terminal_web_chat_turn_saves_attachment_metadata_without_mutating_content():
    manager = FakeMemoryManager()
    llm_client = FakeLLMClient(manager)
    response_handler = FakeResponseHandler(llm_client)
    web_interface = FakeWebInterface()

    mode = TerminalMode.__new__(TerminalMode)
    mode.llm_client = llm_client
    mode.response_handler = response_handler
    mode.web_interface = web_interface
    mode.character_name = "aoi"
    mode._chat_turn_lock = __import__("asyncio").Lock()
    mode._chat_turn_persistence = None

    await mode._process_user_message_web(
        "",
        session_id="session-1",
        client_message_id="client-2",
        attachments=[{"name": "image.png", "mime_type": "image/png"}],
        attachment_context="[添付画像: image.png]",
    )

    saved_user = manager.repository.saved[0]
    assert saved_user.content == ""
    assert saved_user.metadata["client_message_id"] == "client-2"
    assert saved_user.metadata["attachments"] == [
        {"name": "image.png", "mime_type": "image/png"}
    ]
    assert response_handler.seen_message == (
        "添付ファイルを確認してください。\n\n[添付画像: image.png]"
    )
