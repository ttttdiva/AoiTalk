from types import SimpleNamespace

from src.services.trpg_rules import (
    COC7_RULESET_TAG,
    create_pc_state_for_ruleset,
    create_coc_investigator_state,
    evaluate_roll_for_ruleset,
    evaluate_coc6_d100,
    evaluate_coc7_d100,
    get_builtin_ruleset_profile,
    get_coc_gm_rules_brief,
    get_ruleset_gm_rules_brief,
    is_coc_scenario,
    resolve_roll_target_from_state,
)
from src.services.trpg_coc import (
    COC_SHEET_FORMAT,
    coc_target_from_state,
    apply_coc_san_loss,
    is_coc_san_label,
    parse_coc_san_loss,
    normalize_coc_state,
    parse_coc_sheet_text,
)
from src.services.trpg_rule_reference_service import (
    format_rule_reference_context,
    sort_reference_matches,
)
from src.services.trpg_gm_service import _build_gm_input, _looks_like_gm_refusal


def test_coc_scenario_detection_accepts_coc6_tags():
    assert is_coc_scenario(["trpg", "coc6", "毒入りスープ"], "")
    assert "6版" in get_coc_gm_rules_brief(["coc6"], "coc6")


def test_gm_refusal_detection_catches_unusable_reply():
    assert _looks_like_gm_refusal("申し訳ありませんが、そのリクエストにはお応えできません。")
    assert not _looks_like_gm_refusal("第1ラウンドの結果が公開されます。")


def test_coc_scenario_detection_prefers_ruleset_field():
    assert is_coc_scenario([], "horror", "coc6")
    assert "7版" in get_coc_gm_rules_brief([], "horror", "coc7")


def test_builtin_ruleset_profiles_define_runtime_layers():
    generic = get_builtin_ruleset_profile("generic")
    coc7 = get_builtin_ruleset_profile("coc7")
    shinobigami = get_builtin_ruleset_profile("shinobigami")

    assert generic["character_sheet_schema"]["sheet_format"] == "generic_pc_v1"
    assert coc7["system_type"] == "coc"
    assert coc7["dice_rule_schema"]["success"] == "coc7_success_levels"
    assert shinobigami["metadata"]["needs_rulebook_text"]
    assert "汎用TRPG" in get_ruleset_gm_rules_brief([], "", "shinobigami")


def test_coc7_d100_success_levels():
    regular = evaluate_coc7_d100(total=42, target=60)
    hard = evaluate_coc7_d100(total=30, target=60, difficulty="hard")
    extreme_fail = evaluate_coc7_d100(total=13, target=60, difficulty="extreme")

    assert regular["success"]
    assert regular["success_level"] == "regular"
    assert hard["success"]
    assert hard["success_level"] == "hard"
    assert not extreme_fail["success"]


def test_coc7_d100_fumble_threshold_depends_on_target():
    low_skill = evaluate_coc7_d100(total=96, target=40)
    high_skill = evaluate_coc7_d100(total=96, target=60)

    assert low_skill["fumble"]
    assert not high_skill["fumble"]


def test_default_coc_investigator_state_contains_play_fields():
    state = create_coc_investigator_state("探索者", COC7_RULESET_TAG)

    assert state["ruleset"] == COC7_RULESET_TAG
    assert state["sheet_format"] == COC_SHEET_FORMAT
    assert state["hp"] == state["max_hp"]
    assert state["sanity"] > 0
    assert "目星" in state["skills"]
    assert "ccfolia" in state


def test_rule_engine_creates_generic_pc_state_and_resolves_generic_targets():
    state = create_pc_state_for_ruleset("忍者", "shinobigami")
    state["skills"] = {"隠密術": 7}

    assert state["ruleset"] == "shinobigami"
    assert state["sheet_format"] == "generic_pc_v1"
    assert resolve_roll_target_from_state("shinobigami", state, "隠密術") == 7

    result = evaluate_roll_for_ruleset(
        "shinobigami",
        {"count": 2, "faces": 6, "total": 6},
        target=7,
    )
    assert result["success"]
    assert result["details"]["ruleset"] == "shinobigami"


def test_rule_engine_keeps_coc7_and_coc6_success_levels_distinct():
    coc7 = evaluate_roll_for_ruleset(
        "coc7",
        {"count": 1, "faces": 100, "total": 30},
        target=60,
        difficulty="hard",
    )
    coc6 = evaluate_roll_for_ruleset(
        "coc6",
        {"count": 1, "faces": 100, "total": 30},
        target=60,
        difficulty="hard",
    )

    assert coc7["details"]["success_level"] == "hard"
    assert coc6["details"]["ruleset"] == "coc6"
    assert coc6["details"]["success_level"] == "regular"
    assert coc6["details"]["success_label"] == "成功"
    assert coc6["success"]


def test_coc6_d100_reports_special_and_fumble():
    special = evaluate_coc6_d100(total=10, target=60)
    fumble = evaluate_coc6_d100(total=96, target=40)

    assert special["success"]
    assert special["success_level"] == "special"
    assert special["success_label"] == "スペシャル"
    assert not fumble["success"]
    assert fumble["fumble"]
    assert fumble["success_label"] == "ファンブル"


def test_coc_state_is_sheet_format_specific():
    state = normalize_coc_state(
        {
            "characteristics": {"STR": 12, "CON": 13, "POW": 11, "DEX": 14, "SIZ": 10, "INT": 15, "EDU": 16},
            "skills": {"目星": 70, "図書館": 65},
        },
        "探索者",
        "coc6",
    )

    assert state["sheet_format"] == COC_SHEET_FORMAT
    assert state["hp"] == 12
    assert state["sanity"] == 55
    assert coc_target_from_state(state, "目星") == 70
    assert coc_target_from_state(state, "アイデア") == 75
    assert coc_target_from_state(state, "SAN") == 55


def test_coc_san_helpers_parse_and_apply_loss():
    state = normalize_coc_state({"sanity": 55}, "探索者", "coc6")
    loss_pair = parse_coc_san_loss("SAN 0/1d3")

    assert loss_pair == {"success": "0", "failure": "1d3"}
    assert is_coc_san_label("SAN 1/1D6 スープの真相を理解した衝撃")
    updated = apply_coc_san_loss(state, 2)

    assert updated["sanity"] == 53
    assert updated["san"] == 53
    assert any(status["label"] == "SAN" and status["value"] == 53 for status in updated["statuses"])


def test_coc_san_label_accepts_loss_pair_notes():
    assert is_coc_san_label("SAN 1/1D6 スープの真相を理解した衝撃")
    assert is_coc_san_label("SAN 0/1D3")
    assert is_coc_san_label("正気度 1/1D6")
    assert not is_coc_san_label("目星")


def test_parse_coc_sheet_text_accepts_charasheet_like_text():
    state = parse_coc_sheet_text(
        """
        キャラクター名: 佐藤
        職業: 記者
        STR 10 CON 12 POW 13 DEX 14 APP 9 SIZ 11 INT 15 EDU 16
        目星 65%
        聞き耳 55%
        図書館 70%
        """,
        "coc6",
    )

    assert state["name"] == "佐藤"
    assert state["occupation"] == "記者"
    assert state["skills"]["目星"] == 65
    assert state["skills"]["図書館"] == 70


def test_gm_input_uses_structured_trpg_document_without_source_body_escape():
    scenario = SimpleNamespace(
        title="毒入りスープ",
        description="説明欄だけでは進行しない。",
        setting="構造化データで進行する。",
        scenario_kind="trpg",
        ruleset="coc6",
        genre="coc6",
        tags=["trpg", "coc6"],
        characters=[],
        gm_instructions="",
    )
    play_session = SimpleNamespace(perspective="third_person", shared_state={})
    document = SimpleNamespace(
        ruleset="coc6",
        source_label="https://example.invalid/source.txt",
        source_text="これは保存用アーカイブで、GM入力には流さない。",
        structure={
            "nodes": [
                {
                    "id": "room",
                    "type": "location",
                    "title": "導入の部屋",
                    "summary": "閉鎖空間として始まり、テーブルと扉が調査対象になる。",
                }
            ]
        },
    )

    ctx = _build_gm_input(
        play_session=play_session,
        scenario=scenario,
        current_scene=None,
        trpg_document=document,
        participants=[],
        logs=[],
    )

    assert "## TRPG構造化インデックス" in ctx["setting"]
    assert "導入の部屋" in ctx["setting"]
    assert "閉鎖空間として始まり" in ctx["setting"]
    assert "これは保存用アーカイブ" not in ctx["setting"]
    assert "https://example.invalid/source.txt" not in ctx["setting"]


def test_gm_input_includes_ruleset_profile_and_structured_references():
    scenario = SimpleNamespace(
        title="汎用卓",
        scenario_kind="trpg",
        ruleset="shinobigami",
        description="",
        setting="",
        genre="",
        tags=["trpg", "shinobigami"],
        characters=[],
        gm_instructions="",
    )
    play_session = SimpleNamespace(perspective="third_person", shared_state={})
    ctx = _build_gm_input(
        play_session=play_session,
        scenario=scenario,
        current_scene=None,
        trpg_document=None,
        participants=[],
        logs=[],
        ruleset_profile=get_builtin_ruleset_profile("shinobigami"),
        structured_rule_context="## Related Rules\n- 基本判定 (checks)\n  判定は2D6を使う。目標値以上なら成功。",
    )

    assert "## TRPGルールシステム" in ctx["setting"]
    assert "ruleset: shinobigami" in ctx["setting"]
    assert "## Structured TRPG Rule References" in ctx["setting"]
    assert "基本判定" in ctx["setting"]
    assert "判定は2D6を使う" in ctx["setting"]


def test_gm_input_includes_opening_material_and_generic_session_loop():
    scenario = SimpleNamespace(
        title="汎用卓",
        scenario_kind="trpg",
        ruleset="generic",
        description="閉じた部屋から始まる。",
        setting="対話と探索を重視する。",
        opening_text="PCたちは審査室に案内される。",
        genre="",
        tags=["trpg"],
        characters=[],
        gm_instructions="",
    )
    play_session = SimpleNamespace(perspective="third_person", shared_state={})

    ctx = _build_gm_input(
        play_session=play_session,
        scenario=scenario,
        current_scene=None,
        trpg_document=None,
        participants=[],
        logs=[],
    )

    assert "## 導入素材" in ctx["setting"]
    assert "PCたちは審査室に案内される" in ctx["setting"]
    assert "ログへそのまま貼らず" in ctx["setting"]
    assert "## 汎用TRPG進行ループ" in ctx["extra_instructions"]
    assert "## セッション開始時の導入方針" in ctx["extra_instructions"]
    assert "招待状・案内・受付・入室" in ctx["extra_instructions"]
    assert "冒頭1段落には、招待状、案内係、受付、入室" in ctx["extra_instructions"]
    assert "第1ピリオドの秘密投票、集計、有効得票数公開、得点確定" in ctx["extra_instructions"]
    assert "質問、観察、交渉、行動宣言、進行要求" in ctx["extra_instructions"]
    assert "複数フェーズを勝手に飛ばさない" in ctx["extra_instructions"]


def test_structured_reference_context_is_the_gm_rule_source():
    summary = format_rule_reference_context(
        {
            "rules": [
                {
                    "title": "SANチェック",
                    "rule_domain": "sanity",
                    "mechanic_key": "coc_apply_resource",
                    "source_title": "CoC6 structured rules",
                    "raw_excerpt": "SANチェックでは正気度を目標に1D100を振る。",
                }
            ],
            "creatures": [],
        }
    )

    assert "SANチェックでは正気度" in summary
    assert "CoC6 structured rules" in summary

