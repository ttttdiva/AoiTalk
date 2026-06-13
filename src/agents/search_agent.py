"""Search specialist agent."""

from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings

from ..llm.tool_policy import is_memory_search_enabled
from ..tools.adapters import OpenAIAgentAdapter
from ..tools.basic.grok_x_search import grok_x_search
from ..tools.basic.web_search import web_search_with_config
from ..tools.core import tool
from ..tools.memory import search_memory
from ..tools.knowledge import knowledge_search
from .base import BaseAgent


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _x_search_enabled(config: Any) -> bool:
    search_config = _config_get(config, "search", {}) or {}
    if not isinstance(search_config, dict):
        return False
    return bool(
        search_config.get(
            "x_enabled",
            search_config.get("grok_x_enabled", False),
        )
    )


def _knowledge_search_enabled(config: Any) -> bool:
    search_config = _config_get(config, "search", {}) or {}
    if not isinstance(search_config, dict):
        return False
    return bool(search_config.get("knowledge_enabled", False))


class SearchAgent(BaseAgent):
    """Specialized agent for public web, X, memory, and Knowledge search."""

    def __init__(self, model: str = "gpt-4o-mini", config: Any = None):
        super().__init__(model)
        self.config = config

    def _create_agent(self) -> Agent:
        config = self.config

        @tool
        def web_search(query: str) -> str:
            """公開Webを検索する。最新性や時点が重要な一般情報で使う。"""
            return web_search_with_config(query, config=config)

        tools = [web_search]
        if _x_search_enabled(config):
            tools.append(grok_x_search)
        if _knowledge_search_enabled(config):
            tools.append(knowledge_search)
        if is_memory_search_enabled(config):
            tools.append(search_memory)

        instructions = """
あなたは検索専門エージェントです。

ユーザーの依頼に必要な検索だけを実行し、結果を日本語で簡潔にまとめてください。
- 最新性、価格、仕様、法律、手続き、ニュースなど時点依存の一般情報はWeb検索を優先する。
- X/Twitter上の投稿や速報性の高いSNS情報が必要で、X検索ツールが利用可能な場合だけX検索を使う。
- ローカル文書やナレッジに答えがありそうで、Knowledge検索ツールが利用可能な場合だけKnowledge検索を使う。
- 過去の会話履歴が必要な場合だけメモリ検索を使う。
- 検索不要で答えられる場合は、無理に検索しない。
""".strip()

        return Agent(
            name="SearchAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="auto"),
            tools=OpenAIAgentAdapter.convert_all(tools),
        )

    def get_tool_name(self) -> str:
        return "search_assistant"

    def get_tool_description(self) -> str:
        return (
            "Search assistant - public web search, optional X/Twitter search, "
            "Knowledge document search, and memory search"
        )
