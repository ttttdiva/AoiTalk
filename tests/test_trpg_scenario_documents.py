from types import SimpleNamespace

import pytest

from src.services.scenario_service import (
    ScenarioError,
    _normalize_document_payload,
    _validate_trpg_character_nodes,
)
from src.services.trpg_rulebook_importer import (
    build_rulebook_dry_run,
    build_supplement_dry_run,
    build_markdown_rulebook_payload,
    build_text_rulebook_payload,
)
from src.services.trpg_rulebook_service import _rulebook_payload, normalize_rulebook_structure
from src.services.trpg_gm_service import _trpg_structure_summary


def test_trpg_document_payload_keeps_source_text_as_archive_body():
    payload = _normalize_document_payload(
        {
            "ruleset": " coc6 ",
            "source_label": " user provided archive ",
            "source_text": "published scenario body",
            "structure": {
                "format": "published_trpg_scenario",
                "nodes": [
                    {
                        "type": "location",
                        "title": "開始地点",
                        "summary": "導入で扱う場所",
                        "tags": "導入, 探索",
                    }
                ],
            },
        }
    )

    assert payload["ruleset"] == "coc6"
    assert payload["source_label"] == "user provided archive"
    assert payload["source_text"] == "published scenario body"
    assert payload["structure"]["format"] == "published_trpg_scenario"
    assert payload["structure"]["version"] == 1
    assert payload["structure"]["nodes"][0] == {
        "id": "開始地点",
        "type": "location",
        "title": "開始地点",
        "summary": "導入で扱う場所",
        "body": "",
        "tags": ["導入", "探索"],
        "metadata": {},
    }
    assert payload["structure"]["links"] == []
    assert payload["structure"]["metadata"] == {}


def test_trpg_document_payload_defaults_to_generic_ruleset():
    payload = _normalize_document_payload({"source_text": "body", "structure": []})

    assert payload["ruleset"] == "generic"
    assert payload["structure"] == {
        "version": 1,
        "nodes": [],
        "links": [],
        "metadata": {},
    }


def test_trpg_document_payload_rejects_reference_escape_nodes():
    with pytest.raises(ScenarioError, match="自己完結していません"):
        _normalize_document_payload(
            {
                "source_text": "body",
                "structure": {
                    "nodes": [
                        {
                            "id": "npc",
                            "type": "npc",
                            "title": "NPC",
                            "summary": "反応と正体は本文を参照する。",
                        }
                    ]
                },
            }
        )


def test_trpg_document_payload_rejects_empty_structure_nodes():
    with pytest.raises(ScenarioError, match="summary/body/metadata"):
        _normalize_document_payload(
            {
                "source_text": "body",
                "structure": {
                    "nodes": [
                        {
                            "id": "truth",
                            "type": "section",
                            "title": "真相",
                        }
                    ]
                },
            }
        )


def test_trpg_document_payload_rejects_external_source_labels():
    for source_label in [
        "https://example.invalid/source.txt",
        "D:\\Work\\TRPG\\scenario.txt",
        "\\\\server\\share\\scenario.txt",
        "/mnt/data/scenario.txt",
    ]:
        with pytest.raises(ScenarioError, match="外部URLやローカルパス"):
            _normalize_document_payload(
                {
                    "source_label": source_label,
                    "source_text": "body",
                    "structure": {},
                }
            )


def test_trpg_character_nodes_require_character_database_rows():
    structure = _normalize_document_payload(
        {
            "source_text": "body",
            "structure": {
                "nodes": [
                    {
                        "id": "servant",
                        "type": "npc",
                        "title": "下僕の少女",
                        "summary": "卓進行で使うNPC情報。",
                    }
                ]
            },
        }
    )["structure"]

    with pytest.raises(ScenarioError, match="キャラクターDB"):
        _validate_trpg_character_nodes(structure, [])

    with pytest.raises(ScenarioError, match="説明・背景・状態"):
        _validate_trpg_character_nodes(
            structure,
            [SimpleNamespace(id="character-id", name="下僕の少女", description="薄い")],
        )

    _validate_trpg_character_nodes(
        structure,
        [
            SimpleNamespace(
                id="character-id",
                name="下僕の少女",
                description="探索者に接触するNPC。部屋の制約を理解しており、質問に対して警戒しながら反応する。",
                personality_override="不安げだが、決められた役割に従って振る舞う。",
                backstory="この部屋の状況と結末分岐に関わる存在として配置されている。",
                psychology="直接的な協力は避け、探索者の行動を観察する。",
                speech_patterns="短く答え、核心にはすぐ触れない。",
                example_dialogues="",
                trpg_pc_state={},
            )
        ],
    )


def test_trpg_combatant_nodes_require_pc_state():
    structure = _normalize_document_payload(
        {
            "source_text": "body",
            "structure": {
                "nodes": [
                    {
                        "id": "slime",
                        "type": "enemy",
                        "title": "黒いスライム",
                        "summary": "探索地点を守る障害。遭遇時は戦闘または回避で処理する。",
                    }
                ]
            },
        }
    )["structure"]
    rich_enemy = SimpleNamespace(
        id="enemy-id",
        name="黒いスライム",
        description="本を守る敵性存在。接近した探索者に反応して妨害する。",
        personality_override="意思疎通せず、侵入者を排除する。",
        backstory="書物を守る障害として配置されている。",
        psychology="防衛本能だけで動く。",
        speech_patterns="",
        example_dialogues="",
        trpg_pc_state={},
    )

    with pytest.raises(ScenarioError, match="trpg_pc_state"):
        _validate_trpg_character_nodes(structure, [rich_enemy])

    rich_enemy.trpg_pc_state = {"hp": 10, "attacks": [{"name": "接触"}]}
    _validate_trpg_character_nodes(structure, [rich_enemy])


def test_trpg_document_payload_normalizes_generic_nodes_and_links():
    payload = _normalize_document_payload(
        {
            "source_text": "body",
            "structure": {
                "format": "published_trpg_scenario",
                "system": "custom-system",
                "nodes": [
                    {
                        "id": "room-1",
                        "type": "ClockPuzzle",
                        "title": "時計の部屋",
                        "summary": "探索地点",
                        "body": "GM向け補足",
                        "tags": ["探索", "探索", ""],
                        "metadata": {"difficulty": 2},
                    },
                    ["invalid"],
                ],
                "links": [
                    {
                        "source": "room-1",
                        "target": "clue-1",
                        "relation": "Reveals",
                        "condition": {"skill": "目星"},
                    },
                    {"from": "broken"},
                ],
                "metadata": {"source": "manual"},
            },
        }
    )

    structure = payload["structure"]
    assert structure["format"] == "published_trpg_scenario"
    assert structure["system"] == "custom-system"
    assert structure["metadata"] == {"source": "manual"}
    assert structure["nodes"] == [
        {
            "id": "room-1",
            "type": "clockpuzzle",
            "title": "時計の部屋",
            "summary": "探索地点",
            "body": "GM向け補足",
            "tags": ["探索"],
            "metadata": {"difficulty": 2},
        }
    ]
    assert structure["links"] == [
        {
            "from": "room-1",
            "to": "clue-1",
            "relation": "reveals",
            "condition": {"skill": "目星"},
            "metadata": {},
        }
    ]


def test_trpg_structure_summary_feeds_nodes_and_links_to_gm_context():
    summary = _trpg_structure_summary(
        {
            "nodes": [
                {
                    "id": "soup",
                    "type": "item",
                    "title": "毒入りスープ",
                    "summary": "結末分岐に関わる中心アイテム",
                    "tags": ["重要"],
                }
            ],
            "links": [
                {
                    "from": "soup",
                    "to": "ending",
                    "relation": "unlocks",
                }
            ],
        }
    )

    assert "TRPG構造化インデックス" in summary
    assert "[item] 毒入りスープ" in summary
    assert "結末分岐に関わる中心アイテム" in summary
    assert "soup --unlocks--> ending" in summary


def test_rulebook_structure_accepts_generic_rule_nodes():
    structure = normalize_rulebook_structure(
        {
            "nodes": [
                {"id": "roll", "type": "rule", "title": "基本判定"},
                "invalid",
            ],
            "links": [{"from": "roll", "to": "skill"}],
            "metadata": {"source": "ocr"},
        }
    )

    assert structure["version"] == 1
    assert structure["nodes"] == [{"id": "roll", "type": "rule", "title": "基本判定"}]
    assert structure["links"] == [{"from": "roll", "to": "skill"}]
    assert structure["metadata"] == {"source": "ocr"}


def test_rulebook_payload_keeps_requested_document_identity_outside_structure():
    payload = _rulebook_payload(
        {
            "title": "CoC OCR",
            "source_label": "local.txt",
            "source_text": "body",
            "priority": 100,
            "is_active": True,
        },
        "coc6",
    )

    assert payload["ruleset_key"] == "coc6"
    assert payload["title"] == "CoC OCR"
    assert payload["source_label"] == "local.txt"
    assert payload["source_text"] == "body"
    assert payload["priority"] == 100
    assert payload["is_active"]


def test_markdown_supplement_payload_indexes_creature_catalog_headings():
    payload = build_markdown_rulebook_payload(
        """# 神話生物図鑑

## 深きもの
水棲の神話生物。沿岸の事件で扱う。

### 戦闘
爪による攻撃を行う。

## ショゴス
不定形の怪物。
""",
        ruleset_key=" coc6 ",
        title="マレウス・モンストロルム",
        source_label="malleus.md",
        supplement_kind="creature_catalog",
        priority=80,
    )

    assert payload["ruleset_key"] == "coc6"
    assert payload["title"] == "マレウス・モンストロルム"
    assert payload["source_text"].startswith("# 神話生物図鑑")
    assert payload["priority"] == 80

    structure = payload["structure"]
    assert structure["metadata"]["document_type"] == "supplement"
    assert structure["metadata"]["supplement_kind"] == "creature_catalog"
    assert structure["metadata"]["source_format"] == "markdown"
    assert [node["title"] for node in structure["nodes"]] == [
        "神話生物図鑑",
        "深きもの",
        "戦闘",
        "ショゴス",
    ]
    assert structure["nodes"][1]["type"] == "creature"
    assert "水棲の神話生物" in structure["nodes"][1]["summary"]
    assert {
        "from": structure["nodes"][0]["id"],
        "to": structure["nodes"][1]["id"],
        "relation": "contains",
        "metadata": {},
    } in structure["links"]


def test_text_supplement_payload_stores_full_body_without_requiring_markdown():
    payload = build_text_rulebook_payload(
        "神話生物: 深きもの\n沿岸部の事件で使う追加資料。",
        ruleset_key="coc6",
        title="神話生物メモ",
        source_label="user-provided text",
        supplement_kind="creature_catalog",
        source_format="text",
    )

    assert payload["source_text"] == "神話生物: 深きもの\n沿岸部の事件で使う追加資料。"
    assert payload["structure"]["nodes"] == []
    assert payload["structure"]["links"] == []
    assert payload["structure"]["metadata"]["source_format"] == "text"
    assert payload["structure"]["metadata"]["document_type"] == "supplement"
    assert payload["structure"]["metadata"]["supplement_kind"] == "creature_catalog"


def test_rulebook_dry_run_reports_domains_mechanics_and_planned_writes():
    dry_run = build_rulebook_dry_run(
        """# SAN check
Roll 1d100 against SAN. On failure apply 1/1D6 sanity loss.

# Combat
Attack rolls use weapon skill and damage.
""",
        ruleset_key="coc6",
        title="CoC sample",
        source_label="sample.txt",
    )

    assert dry_run["mode"] == "dry-run"
    assert dry_run["extraction_count"] == 2
    assert dry_run["planned_writes"]["trpg_reference_documents"] == 1
    assert dry_run["planned_writes"]["trpg_rule_items"] == 2
    assert dry_run["planned_writes"]["trpg_rulebook_documents"] == 0
    assert dry_run["mechanic_key_summary"]["coc_apply_resource"] >= 1
    assert "Combat" in dry_run["headings"]
    assert dry_run["samples"][0]["raw_excerpt"].startswith("SAN check")


def test_supplement_dry_run_extracts_creature_runtime_fields():
    dry_run = build_supplement_dry_run(
        """# Deep One
STR 12 CON 13 POW 11 HP 13
Attack: claws 1D6 damage
SAN 0/1D6
""",
        ruleset_key="coc6",
        title="Malleus sample",
        source_label="malleus.txt",
        supplement_kind="creature_catalog",
    )

    assert dry_run["planned_writes"]["trpg_reference_documents"] == 1
    assert dry_run["planned_writes"]["trpg_supplement_documents"] == 0
    assert dry_run["planned_writes"]["trpg_creature_entries"] == 1
    entry = dry_run["entries"][0]
    assert entry["name"] == "Deep One"
    assert entry["characteristics"]["STR"] == 12
    assert entry["san_loss"] == "0/1D6"
    assert "coc_attack_action" in entry["mechanic_links"]
