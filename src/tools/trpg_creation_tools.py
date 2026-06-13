"""TRPG scenario creation tools for runtime chat."""

from __future__ import annotations

import json
from typing import Any, Callable

from .core import tool as tool_decorator


def _parse_json_value(raw: str, *, fallback: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONの形式が不正です: {exc.msg}") from exc


def _ensure_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} はJSON配列で指定してください")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{field_name}[{index}] はJSONオブジェクトで指定してください")
        normalized.append(item)
    return normalized


def _coerce_tags(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    raise ValueError("tags_json はJSON配列、またはカンマ区切り文字列で指定してください")


def _compact_text(value: Any, limit: int = 260) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _build_default_structure(
    *,
    title: str,
    setting: str,
    opening_text: str,
    gm_instructions: str,
    characters: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    if setting or opening_text or gm_instructions:
        nodes.append(
            {
                "id": "overview",
                "type": "overview",
                "title": title or "シナリオ概要",
                "summary": _compact_text(setting or opening_text or gm_instructions),
                "body": "\n\n".join(
                    part
                    for part in [
                        f"舞台設定: {setting}" if setting else "",
                        f"導入: {opening_text}" if opening_text else "",
                        f"GM指示: {gm_instructions}" if gm_instructions else "",
                    ]
                    if part
                ),
                "tags": ["overview"],
                "metadata": {},
            }
        )

    for index, scene in enumerate(scenes):
        scene_id = str(scene.get("id") or f"scene-{index + 1}")
        nodes.append(
            {
                "id": scene_id,
                "type": str(scene.get("scene_type") or scene.get("type") or "scene"),
                "title": str(scene.get("title") or f"シーン{index + 1}"),
                "summary": _compact_text(scene.get("description") or scene.get("summary")),
                "body": str(
                    scene.get("gm_instructions")
                    or scene.get("content")
                    or scene.get("body")
                    or ""
                ),
                "tags": scene.get("tags") if isinstance(scene.get("tags"), list) else [],
                "metadata": {},
            }
        )
        if index > 0:
            links.append(
                {
                    "from": str(scenes[index - 1].get("id") or f"scene-{index}"),
                    "to": scene_id,
                    "relation": "next",
                    "condition": {},
                    "metadata": {},
                }
            )

    for index, character in enumerate(characters):
        role = str(character.get("role") or "npc").lower()
        name = str(character.get("name") or f"NPC{index + 1}")
        nodes.append(
            {
                "id": f"character-{index + 1}",
                "type": "character",
                "title": name,
                "summary": _compact_text(character.get("description")),
                "body": "\n".join(
                    part
                    for part in [
                        str(character.get("personality_override") or ""),
                        str(character.get("backstory") or ""),
                        str(character.get("speech_patterns") or ""),
                    ]
                    if part
                ),
                "tags": ["character", role],
                "metadata": {"character_name": name},
            }
        )

    return {"version": 1, "nodes": nodes, "links": links, "metadata": {}}


async def _call_service(name: str, *args: Any, **kwargs: Any) -> Any:
    from ..services import scenario_service

    fn: Callable[..., Any] = getattr(scenario_service, name)
    return await fn(*args, **kwargs)


@tool_decorator
async def create_trpg_scenario(
    title: str,
    description: str,
    setting: str,
    opening_text: str = "",
    gm_instructions: str = "",
    ruleset: str = "generic",
    genre: str = "TRPG",
    perspective: str = "third_person",
    tags_json: str = "[]",
    characters_json: str = "[]",
    scenes_json: str = "[]",
    trpg_structure_json: str = "",
    source_text: str = "",
    source_label: str = "AI生成シナリオ",
) -> dict[str, Any]:
    """チャットで依頼されたTRPGシナリオを作成して保存する。ユーザーが「TRPGシナリオを作って」「こういうシナリオを保存して」など、AI生成結果をシナリオ一覧で使える形にしたい場合に使う。

    Args:
        title: シナリオタイトル
        description: シナリオの概要
        setting: 舞台設定・世界観
        opening_text: セッション開始時に提示する導入文
        gm_instructions: AI GM / GM 向けの進行メモ、秘匿、勝敗条件
        ruleset: TRPGシステム。generic, coc6, coc7, shinobigami, swordworld2_5 のいずれか
        genre: ジャンル
        perspective: first_person または third_person
        tags_json: タグのJSON配列。例: ["trpg", "ホラー"]
        characters_json: NPC/敵/味方のJSON配列。name, role, description, personality_override, backstory, speech_patterns, trpg_pc_state を指定可能
        scenes_json: シーンのJSON配列。title, description, scene_type, gm_instructions, transitions, content を指定可能
        trpg_structure_json: TRPG本文タブで使う構造化JSON。省略時は characters/scenes から自動生成する
        source_text: TRPG本文として保存するシナリオ全文・進行メモ
        source_label: 出典ラベル。外部URLやローカルパスではなく「AI生成シナリオ」などを指定
    """
    try:
        normalized_title = str(title or "").strip()
        if not normalized_title:
            return {"success": False, "message": "タイトルは必須です"}

        tags = _coerce_tags(tags_json)
        characters = _ensure_list(
            _parse_json_value(characters_json, fallback=[]),
            "characters_json",
        )
        scenes = _ensure_list(
            _parse_json_value(scenes_json, fallback=[]),
            "scenes_json",
        )
        for index, character in enumerate(characters):
            if not str(character.get("name") or "").strip():
                raise ValueError(f"characters_json[{index}].name は必須です")
        for index, scene in enumerate(scenes):
            if not str(scene.get("title") or "").strip():
                raise ValueError(f"scenes_json[{index}].title は必須です")
        structure = _parse_json_value(trpg_structure_json, fallback=None)
        if structure is None:
            structure = _build_default_structure(
                title=normalized_title,
                setting=setting,
                opening_text=opening_text,
                gm_instructions=gm_instructions,
                characters=characters,
                scenes=scenes,
            )
        if not isinstance(structure, dict):
            raise ValueError("trpg_structure_json はJSONオブジェクトで指定してください")

        scenario = await _call_service(
            "create_scenario",
            {
                "title": normalized_title,
                "scenario_kind": "trpg",
                "ruleset": ruleset or "generic",
                "description": description,
                "genre": genre or "TRPG",
                "perspective": perspective or "third_person",
                "setting": setting,
                "opening_text": opening_text,
                "gm_instructions": gm_instructions,
                "tags": tags,
            },
        )
        scenario_id = scenario["id"]

        created_characters = []
        for index, character in enumerate(characters):
            payload = dict(character)
            payload.setdefault("sort_order", index)
            created_characters.append(
                await _call_service("add_scenario_character", scenario_id, payload)
            )

        created_scenes = []
        for index, scene in enumerate(scenes):
            payload = dict(scene)
            payload.setdefault("sort_order", index)
            created_scenes.append(
                await _call_service("add_scenario_scene", scenario_id, payload)
            )

        warnings = []
        document = None
        if source_text.strip() or structure.get("nodes"):
            try:
                document = await _call_service(
                    "upsert_trpg_document",
                    scenario_id,
                    {
                        "ruleset": ruleset or "generic",
                        "source_label": source_label or "AI生成シナリオ",
                        "source_text": source_text,
                        "structure": structure,
                    },
                )
            except Exception as exc:
                warnings.append(f"TRPG本文/構造メモの保存に失敗しました: {exc}")

        return {
            "success": True,
            "message": "TRPGシナリオを作成しました",
            "scenario": scenario,
            "characters": created_characters,
            "scenes": created_scenes,
            "trpg_document": document,
            "warnings": warnings,
            "links": {
                "scenario_list": "/scenarios",
                "create_room": f"/trpg?scenario_id={scenario_id}&create=1",
            },
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"TRPGシナリオ作成エラー: {exc}",
        }
