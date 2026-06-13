import socket
from types import SimpleNamespace

from src.api.server import WebChatServer
from src.assistant.base import BaseAssistant
from src.api.web_interface import WebChatInterface


class _TestAssistant(BaseAssistant):
    async def _initialize_mode_specific(self) -> bool:
        return True

    async def run(self):
        return None

    async def _cleanup_mode_specific(self):
        return None


def test_web_interface_reports_occupied_port_without_starting():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        interface = object.__new__(WebChatInterface)
        interface.is_running = True

        assert interface.start_server(host="127.0.0.1", port=port) is None
        assert interface.is_running is False


def test_web_server_notifies_when_llm_client_changes():
    server = object.__new__(WebChatServer)
    server._heartbeat_runner = None
    server.on_llm_client_change = None
    seen = []
    client = object()

    server.set_llm_client_change_callback(seen.append)
    server.set_llm_client(client)

    assert server._llm_client is client
    assert seen == [client]


def test_activate_llm_client_updates_response_handler_and_prompt():
    assistant = object.__new__(_TestAssistant)
    assistant.character_config = {"personality": {"details": "system prompt"}}
    assistant.web_interface = None
    assistant.response_handler = SimpleNamespace(llm_client="old")
    prompts = []

    class Client:
        def set_system_prompt(self, prompt):
            prompts.append(prompt)

    client = Client()

    BaseAssistant._activate_llm_client(assistant, client)

    assert assistant.llm_client is client
    assert assistant.response_handler.llm_client is client
    assert prompts == ["system prompt"]
