from src.services.trpg_rule_reference_service import (
    format_rule_reference_context,
    infer_rule_domain_and_mechanic,
    sort_reference_matches,
)
from src.services.trpg_rulebook_importer import build_rulebook_dry_run, build_supplement_dry_run


def test_structured_rule_reference_formatter_and_ranker():
    items = [
        {
            "title": "Combat",
            "rule_domain": "combat",
            "mechanic_key": "coc_attack_action",
            "raw_excerpt": "Attack and damage procedure.",
            "confidence": 0.7,
            "priority": 1,
        },
        {
            "title": "SAN check",
            "rule_domain": "sanity",
            "mechanic_key": "coc_apply_resource",
            "raw_excerpt": "SAN loss on failed check.",
            "confidence": 0.9,
            "priority": 1,
        },
    ]

    ranked = sort_reference_matches(items, "SAN loss")
    assert ranked[0]["title"] == "SAN check"

    context = format_rule_reference_context({"rules": ranked, "creatures": []})
    assert "Related Rules" in context
    assert "coc_apply_resource" in context


def test_lovecraft_story_fragment_is_not_imported_as_reviewed_spell_rule():
    text = """
ておくために、エスキモーの悪魔主義者から教授の採取した呪文をできるかぎ
り思いだしてほしいと、手もあわさんばかりにたのみこんだ。そのあと細部にい
たるまでの徹底した綿密な照合がおこなわれた。

ふんぐるい むぐるうなふ くとうるう るるいえ うがなぐる ふたぐん
"""

    dry_run = build_rulebook_dry_run(
        text,
        ruleset_key="coc6",
        title="CoC OCR",
        source_label="ocr.txt",
    )

    assert dry_run["extraction_count"] >= 1
    for item in dry_run["items"]:
        assert item["mechanic_key"] == ""
        assert item["needs_review"]
        assert item["confidence"] < 0.75


def test_spell_keyword_alone_does_not_link_to_spell_cost_mechanic():
    result = infer_rule_domain_and_mechanic(
        "声をあげて唱えられた呪文に、分節から推定される区切りをつける。"
    )

    assert result["rule_domain"] == "spells"
    assert result["mechanic_key"] == ""
    assert result["confidence"] < 0.75


def test_actual_spell_rule_with_cost_links_to_spell_mechanic():
    result = infer_rule_domain_and_mechanic(
        "この呪文をかけるためには4マジック・ポイントのコストがかかり、詠唱には3ラウンド必要である。"
    )

    assert result["rule_domain"] == "spells"
    assert result["mechanic_key"] == "coc_spell_cost_action"


def test_malleus_creature_catalog_uses_stat_blocks_as_entries():
    text = """
前書き
これは項目ではない。

アイホートの後裔、迷路の神に奉仕する集合生命体
能力値 ロール 平均値
STR 2D6+10 17
CON 3D6+6 16
SIZ 2D6+6 13
INT 3D6+6 16
POW 3D6+6 16
DEX 2D6+6 13
武器: 基本命中率、ダメージ 武器による
正気度喪失: アイホートの後裔を見て失う正気度ポイントは 0/1D6

クトゥルフ神話の神格
Deities of the Mythos
アザトース、沸騰する混沌の中心
STR 該当せず CON 300 SIZ さまざま INT 0 POW 100
DEX 該当せず 移動 0 耐久力 300
武器: 偽足 100%、ダメージ 1D100
正気度喪失: アザトースを見て失う正気度ポイントは 1D10/1D100
"""

    result = build_supplement_dry_run(
        text,
        ruleset_key="coc6",
        title="マレウス",
        source_label="sample",
        supplement_kind="creature_catalog",
    )

    assert result["extraction_count"] == 2
    assert [entry["name"] for entry in result["entries"]] == ["アイホートの後裔", "アザトース"]
    assert result["entries"][0]["entry_type"] == "creature"
    assert result["entries"][0]["characteristics"]["STR"] == 17
    assert result["entries"][1]["entry_type"] == "deity"
    assert result["entries"][1]["characteristics"]["CON"] == 300


def test_malleus_creature_catalog_starts_entries_at_description_header():
    text = """
前の項目の末尾

Fishers from Outside
あの世からの漁夫
下級の奉仕種族
漁夫そのものの説明文。
攻撃: 漁夫は空から襲う。

あの世からの漁夫、グロス＝ゴールカの従者
能力値 ロール 平均値
STR 3D6+15 25
CON 2D6+6 13
SIZ 3D6+20 30
INT 3D6 10
POW 3D6 10
DEX 3D6+6 16
正気度喪失: あの世からの漁夫を見て失う正気度ポイントは 0/1D6

<!-- page: 0022 -->
Spawn of Aboth
アブホースの落とし子
下級の奉仕種族
アブホースの落とし子そのものの説明文。

アブホースの落とし子、外なる神の痕跡
能力値 ロール 平均値
STR 1D10 5
CON 1D10 5
SIZ 1D10 5
INT 1D10 5
POW 1D10 5
DEX 1D10 5
正気度喪失: アブホースの落とし子を見て失う正気度ポイントは 0/1D4
"""

    result = build_supplement_dry_run(
        text,
        ruleset_key="coc6",
        title="マレウス",
        source_label="sample",
        supplement_kind="creature_catalog",
    )

    assert [entry["name"] for entry in result["entries"]] == ["あの世からの漁夫", "アブホースの落とし子"]
    first, second = result["entries"]
    assert first["summary"].startswith("Fishers from Outside")
    assert "漁夫そのものの説明文" in first["source_excerpt"]
    assert "Spawn of Aboth" not in first["source_excerpt"]
    assert second["summary"].startswith("Spawn of Aboth")
    assert "アブホースの落とし子そのものの説明文" in second["source_excerpt"]
