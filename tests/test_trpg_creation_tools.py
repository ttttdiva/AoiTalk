from __future__ import annotations

import json

import pytest

from src.tools.trpg_creation_tools import create_trpg_scenario


@pytest.mark.asyncio
async def test_create_trpg_scenario_persists_chat_generated_payload(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def fake_create_scenario(payload):
        calls.append(("create_scenario", payload))
        return {"id": "scenario-1", **payload}

    async def fake_add_scenario_character(scenario_id, payload):
        calls.append(("add_scenario_character", {"scenario_id": scenario_id, **payload}))
        count = len([c for c in calls if c[0] == "add_scenario_character"])
        return {"id": f"char-{count}", **payload}

    async def fake_add_scenario_scene(scenario_id, payload):
        calls.append(("add_scenario_scene", {"scenario_id": scenario_id, **payload}))
        count = len([c for c in calls if c[0] == "add_scenario_scene"])
        return {"id": f"scene-{count}", **payload}

    async def fake_upsert_trpg_document(scenario_id, payload):
        calls.append(("upsert_trpg_document", {"scenario_id": scenario_id, **payload}))
        return {"id": "doc-1", **payload}

    monkeypatch.setattr(
        "src.services.scenario_service.create_scenario",
        fake_create_scenario,
    )
    monkeypatch.setattr(
        "src.services.scenario_service.add_scenario_character",
        fake_add_scenario_character,
    )
    monkeypatch.setattr(
        "src.services.scenario_service.add_scenario_scene",
        fake_add_scenario_scene,
    )
    monkeypatch.setattr(
        "src.services.scenario_service.upsert_trpg_document",
        fake_upsert_trpg_document,
    )

    result = await create_trpg_scenario.execute_async(
        title="雨の館",
        description="雨に閉ざされた館で失踪事件を追うホラーTRPG。",
        setting="山間の古い洋館。外は豪雨で通信が途絶えている。",
        opening_text="探索者たちは依頼人の手紙を頼りに館へ到着する。",
        gm_instructions="執事は真相を隠している。時計塔の鐘が進行トリガー。",
        ruleset="coc6",
        genre="ホラー",
        tags_json="密室, 調査",
        characters_json=json.dumps(
            [
                {
                    "name": "黒井 透",
                    "role": "npc",
                    "description": "館の執事。礼儀正しいが核心を避ける。",
                }
            ],
            ensure_ascii=False,
        ),
        scenes_json=json.dumps(
            [
                {
                    "title": "玄関ホール",
                    "description": "濡れた足跡が階段へ続いている。",
                    "scene_type": "normal",
                    "gm_instructions": "足跡を追うと書斎へ進む。",
                }
            ],
            ensure_ascii=False,
        ),
        source_text="導入、調査、真相、結末までの進行メモ。",
    )

    assert result["success"] is True
    assert result["scenario"]["scenario_kind"] == "trpg"
    assert result["scenario"]["ruleset"] == "coc6"
    assert result["links"]["create_room"] == "/trpg?scenario_id=scenario-1&create=1"

    assert calls[0] == (
        "create_scenario",
        {
            "title": "雨の館",
            "scenario_kind": "trpg",
            "ruleset": "coc6",
            "description": "雨に閉ざされた館で失踪事件を追うホラーTRPG。",
            "genre": "ホラー",
            "perspective": "third_person",
            "setting": "山間の古い洋館。外は豪雨で通信が途絶えている。",
            "opening_text": "探索者たちは依頼人の手紙を頼りに館へ到着する。",
            "gm_instructions": "執事は真相を隠している。時計塔の鐘が進行トリガー。",
            "tags": ["密室", "調査"],
        },
    )
    assert calls[1][0] == "add_scenario_character"
    assert calls[2][0] == "add_scenario_scene"
    assert calls[3][0] == "upsert_trpg_document"
    document_payload = calls[3][1]
    assert isinstance(document_payload, dict)
    assert document_payload["scenario_id"] == "scenario-1"
    assert document_payload["ruleset"] == "coc6"
    assert document_payload["structure"]["nodes"][0]["id"] == "overview"


@pytest.mark.asyncio
async def test_create_trpg_scenario_rejects_invalid_json():
    result = await create_trpg_scenario.execute_async(
        title="壊れたJSON",
        description="",
        setting="",
        characters_json="{bad",
    )

    assert result["success"] is False
    assert "JSONの形式が不正です" in result["message"]
