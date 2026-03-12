"""XSearchAPIClient のユニットテスト"""

import json
import pytest

from src.tools.external.x_search_mcp.api_client import (
    parse_handles,
    validate_iso_date,
    extract_text_from_response,
    XSearchAPIClient,
)


class TestParseHandles:
    def test_empty_string(self):
        assert parse_handles("") == []

    def test_single_handle(self):
        assert parse_handles("elonmusk") == ["elonmusk"]

    def test_single_handle_with_at(self):
        assert parse_handles("@elonmusk") == ["elonmusk"]

    def test_multiple_handles(self):
        assert parse_handles("elonmusk, OpenAI, @anthropic") == [
            "elonmusk",
            "OpenAI",
            "anthropic",
        ]

    def test_fullwidth_at(self):
        assert parse_handles("＠elonmusk") == ["elonmusk"]

    def test_whitespace_handling(self):
        assert parse_handles("  elonmusk , , OpenAI  ") == ["elonmusk", "OpenAI"]


class TestValidateIsoDate:
    def test_empty_returns_none(self):
        assert validate_iso_date("", "test") is None

    def test_valid_date(self):
        assert validate_iso_date("2026-02-24", "test") == "2026-02-24"

    def test_valid_datetime(self):
        assert validate_iso_date("2026-02-24T10:00:00", "test") == "2026-02-24T10:00:00"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_iso_date("24-02-2026", "from_date")

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_iso_date("not-a-date", "to_date")


class TestExtractTextFromResponse:
    def test_message_with_text(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "text", "text": "結果テキスト"}],
                }
            ]
        }
        assert extract_text_from_response(payload) == "結果テキスト"

    def test_message_with_output_text_type(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "出力テキスト"}],
                }
            ]
        }
        assert extract_text_from_response(payload) == "出力テキスト"

    def test_multiple_messages(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "text", "text": "行1"},
                        {"type": "text", "text": "行2"},
                    ],
                }
            ]
        }
        assert extract_text_from_response(payload) == "行1\n行2"

    def test_fallback_output_text_string(self):
        payload = {"output": [], "output_text": "フォールバック"}
        assert extract_text_from_response(payload) == "フォールバック"

    def test_fallback_output_text_list(self):
        payload = {"output": [], "output_text": ["行A", "行B"]}
        assert extract_text_from_response(payload) == "行A\n行B"

    def test_fallback_json(self):
        payload = {"output": [], "data": "test"}
        result = extract_text_from_response(payload)
        parsed = json.loads(result)
        assert parsed["data"] == "test"

    def test_skips_non_message_types(self):
        payload = {
            "output": [
                {"type": "tool_call", "content": [{"type": "text", "text": "無視"}]},
                {
                    "type": "message",
                    "content": [{"type": "text", "text": "採用"}],
                },
            ]
        }
        assert extract_text_from_response(payload) == "採用"


class TestXSearchAPIClient:
    def test_init_defaults(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        monkeypatch.delenv("XAI_API_BASE", raising=False)
        monkeypatch.delenv("XAI_GROK_MODEL", raising=False)
        client = XSearchAPIClient()
        assert client.api_key == ""
        assert client.api_base == "https://api.x.ai/v1"
        assert client.model == "grok-4-0709"

    def test_init_with_xai_key(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key-123")
        client = XSearchAPIClient()
        assert client.api_key == "test-key-123"

    def test_init_with_grok_key_fallback(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.setenv("GROK_API_KEY", "grok-key-456")
        client = XSearchAPIClient()
        assert client.api_key == "grok-key-456"

    def test_headers(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "my-key")
        client = XSearchAPIClient()
        headers = client.headers
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_search_no_api_key(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        client = XSearchAPIClient()
        result = await client.search("test", system_prompt="test prompt")
        assert "XAI_API_KEY" in result
