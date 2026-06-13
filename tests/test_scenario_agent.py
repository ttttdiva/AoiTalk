from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.llm.specialist_delegate import ScenarioDelegationRunner
from src.tools.registry import ToolRegistry


class FakeCLIBackend:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def execute_prompt(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: int = 300,
        extra_args=None,
        system_context=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "system_context": system_context,
            }
        )
        if not self._responses:
            return True, "No more responses"
        return True, self._responses.pop(0)

    def parse_tool_calls(self, cli_output: str):
        from src.tools.adapters import CLIAdapter

        return CLIAdapter.parse_tool_calls(cli_output)


@pytest.mark.asyncio
async def test_scenario_agent_extracts_items_and_flags_silently():
    config = {
        "llm_provider": "codex-cli",
        "llm_model": "gpt-5-codex",
        "agents": {
            "provider": "codex-cli",
            "model": "gpt-5-codex",
        }
    }
    runner = ScenarioDelegationRunner(config)

    # ToolRegistry をモック化して、テスト用のツールを登録
    registry = ToolRegistry()
    runner._tool_registry = registry

    from src.tools.scenario_tools import get_scenario_state, update_scenario_state

    registry.register(get_scenario_state)
    registry.register(update_scenario_state)

    # LLMの挙動をシミュレート
    # 現状の _run_via_cli は1ターンのツール実行のみサポートするため、
    # 必要なツールをまとめて呼び出す形式にする
    runner.cli_backend = FakeCLIBackend(
        [
            '[TOOL_CALL: update_scenario_state(conversation_id="conv-123", add_items=["rusty key"], add_flags=["found_rusty_key"])]',
            "I've updated your inventory with the rusty key.",
        ]
    )

    # ツール内のDB呼び出しをモック
    mock_session = {
        "id": "play-123",
        "player_state": {"inventory": [], "flags": []},
        "current_scene": {"title": "Dusty Room", "transitions": []},
    }

    with patch(
        "src.tools.scenario_tools.get_play_session_by_conversation_id",
        new=AsyncMock(return_value=mock_session),
    ), patch(
        "src.tools.scenario_tools.update_play_state",
        new=AsyncMock(return_value={"success": True}),
    ) as mock_update:
        # 実行（同期メソッドだが内部でasyncio.run等を呼ぶため、スレッドセーフな実行を確認）
        # テスト環境では asyncio.run がネストする可能性があるため、直接 _run_async を呼ぶか
        # 慎重にテストする。ここでは _run_async を直接テストするのが確実。
        
        result = await runner._run_async(
            "The player picks up a rusty key from the dusty floor.",
            project_context={"conversation_id": "conv-123"}
        )

        assert "rusty key" in result or "inventory" in result.lower()
        
        # update_scenario_state が正しい引数で呼ばれたか
        # 実際には CLIAdapter.execute_tool_calls を経由して呼ばれる
        # mock_update は update_play_state をラップしている
        mock_update.assert_called_once()
        args, _ = mock_update.call_args
        updates = args[1]
        assert "inventory" in updates["player_state"]
        assert "rusty key" in updates["player_state"]["inventory"]
        assert "found_rusty_key" in updates["player_state"]["flags"]


@pytest.mark.asyncio
async def test_scenario_agent_handles_scene_transition_and_bgm():
    config = {
        "llm_provider": "codex-cli",
        "llm_model": "gpt-5-codex",
        "agents": {
            "provider": "codex-cli",
            "model": "gpt-5-codex",
        }
    }
    runner = ScenarioDelegationRunner(config)
    registry = ToolRegistry()
    runner._tool_registry = registry

    from src.tools.scenario_tools import get_scenario_state, update_scenario_state
    from src.tools.entertainment.music_tools import play_bgm

    registry.register(get_scenario_state)
    registry.register(update_scenario_state)
    registry.register(play_bgm)

    runner.cli_backend = FakeCLIBackend(
        [
            '[TOOL_CALL: update_scenario_state(conversation_id="conv-456", current_scene_id="scene-boss")] [TOOL_CALL: play_bgm(bgm_id="battle_theme", volume=0.7)]',
            "You enter the boss chamber. A tense music starts playing.",
        ]
    )

    mock_session = {
        "id": "play-456",
        "current_scene_id": "scene-entrance",
        "current_scene": {
            "title": "Entrance",
            "transitions": [{"condition": "open door", "target_scene_id": "scene-boss"}]
        },
        "player_state": {"hp": 100},
    }

    with patch(
        "src.tools.scenario_tools.get_play_session_by_conversation_id",
        new=AsyncMock(return_value=mock_session),
    ), patch(
        "src.tools.scenario_tools.update_play_state",
        new=AsyncMock(return_value={"success": True}),
    ) as mock_update, patch(
        "src.tools.entertainment.music_tools.trigger_bgm_change",
        new=AsyncMock(return_value=None),
    ) as mock_bgm:
        
        await runner._run_async(
            "I open the heavy iron door at the end of the hallway.",
            project_context={"conversation_id": "conv-456"}
        )

        # シーン移動の確認
        mock_update.assert_called_once()
        args, _ = mock_update.call_args
        assert args[1]["current_scene_id"] == "scene-boss"
        
        # BGM切り替えの確認
        mock_bgm.assert_called_once_with("battle_theme", 0.7)
