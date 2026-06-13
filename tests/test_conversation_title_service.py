import inspect
from types import SimpleNamespace

import pytest

from src.services.conversation_title_service import (
    ensure_conversation_title,
    fallback_title_from_first_message,
    generate_conversation_title,
)


def msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content)


class FakeRepo:
    def __init__(self, messages, title=""):
        self.session = SimpleNamespace(title=title, context={})
        self.messages = messages
        self.updated_title = None
        self.updated_source = None

    async def get_session_by_id(self, session_id, with_messages=False):
        return self.session

    async def get_active_branch_messages(self, session_id):
        return self.messages

    async def update_session_title(self, session_id, title, source=None):
        self.updated_title = title
        self.updated_source = source
        self.session.title = title
        if source:
            self.session.context["title_generation"] = {"source": source}
        return True


@pytest.mark.asyncio
async def test_generates_title_from_initial_exchange_with_llm():
    messages = [
        msg("user", "来週の展示会に向けて、搬入スケジュールと担当者を整理したい"),
        msg("assistant", "展示会準備のタスクを整理しましょう。"),
    ]

    async def generator(prompt: str):
        assert "来週の展示会" in prompt
        assert "展示会準備のタスク" in prompt
        return "展示会準備"

    result = await generate_conversation_title(messages, generator)

    assert result is not None
    assert result.title == "展示会準備"
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_does_not_generate_before_context_is_available():
    result = await generate_conversation_title(
        [msg("user", "この文章だけではまだ早い")],
        lambda _prompt: "未使用",
    )

    assert result is None


@pytest.mark.asyncio
async def test_falls_back_to_first_user_message_when_llm_fails():
    messages = [
        msg("user", "会議メモを要約して次のアクションを出して"),
        msg("assistant", "承知しました。"),
    ]

    async def generator(_prompt: str):
        raise RuntimeError("llm unavailable")

    result = await generate_conversation_title(messages, generator)

    assert result is not None
    assert result.title == "会議メモを要約して次のアクションを出して"
    assert result.source == "fallback"


@pytest.mark.asyncio
async def test_ensure_title_updates_only_blank_session_title():
    repo = FakeRepo(
        [
            msg("user", "TRPGの導入シーンを相談したい"),
            msg("assistant", "導入シーンの雰囲気から決めましょう。"),
        ]
    )

    result = await ensure_conversation_title(
        repo=repo,
        session_id="session-1",
        llm_generator=lambda _prompt: "TRPG導入相談",
    )

    assert result is not None
    assert repo.updated_title == "TRPG導入相談"
    assert repo.updated_source == "llm"


@pytest.mark.asyncio
async def test_ensure_title_keeps_existing_title():
    repo = FakeRepo(
        [msg("user", "新しい話題"), msg("assistant", "はい。")],
        title="既存タイトル",
    )

    result = await ensure_conversation_title(
        repo=repo,
        session_id="session-1",
        llm_generator=lambda _prompt: "新タイトル",
    )

    assert result is None
    assert repo.updated_title is None


@pytest.mark.asyncio
async def test_ensure_title_marks_fallback_as_replaceable():
    repo = FakeRepo(
        [
            msg("user", "今日の天気と予定を相談したい"),
            msg("assistant", "予定と天気を合わせて整理しましょう。"),
        ]
    )

    async def generator(_prompt: str):
        raise RuntimeError("llm unavailable")

    result = await ensure_conversation_title(
        repo=repo,
        session_id="session-1",
        llm_generator=generator,
    )

    assert result is not None
    assert result.title == "今日の天気と予定を相談したい"
    assert result.source == "fallback"
    assert repo.session.context["title_generation"] == {"source": "fallback"}


@pytest.mark.asyncio
async def test_ensure_title_replaces_previous_fallback_with_llm_title():
    repo = FakeRepo(
        [
            msg("user", "今日の天気と予定を相談したい"),
            msg("assistant", "予定と天気を合わせて整理しましょう。"),
            msg("user", "午後の外出もある"),
            msg("assistant", "午後の外出を中心に考えます。"),
        ],
        title="今日の天気と予定を相談したい",
    )
    repo.session.context["title_generation"] = {"source": "fallback"}

    result = await ensure_conversation_title(
        repo=repo,
        session_id="session-1",
        llm_generator=lambda _prompt: "天気と外出予定",
    )

    assert result is not None
    assert repo.updated_title == "天気と外出予定"
    assert repo.updated_source == "llm"


@pytest.mark.asyncio
async def test_ensure_title_repairs_legacy_first_message_fallback_title():
    repo = FakeRepo(
        [
            msg("user", "Hey 今何時"),
            msg("assistant", "現在時刻を確認しました。"),
        ],
        title="Hey 今何時",
    )

    result = await ensure_conversation_title(
        repo=repo,
        session_id="session-1",
        llm_generator=lambda _prompt: "現在時刻確認",
    )

    assert result is not None
    assert repo.updated_title == "現在時刻確認"
    assert repo.updated_source == "llm"


def test_fallback_title_compacts_and_truncates_first_message():
    assert fallback_title_from_first_message("  改行\nを 含む   メッセージ  ") == (
        "改行 を 含む メッセージ"
    )
    assert fallback_title_from_first_message("あ" * 60) == "あ" * 37 + "..."


def test_memory_manager_repository_supports_title_generation_contract():
    from src.memory.manager import ConversationMemoryManager

    repo = ConversationMemoryManager().repository

    assert callable(getattr(repo, "get_active_branch_messages", None))
    assert callable(getattr(repo, "update_session_title", None))
    assert "with_messages" in inspect.signature(repo.get_session_by_id).parameters
