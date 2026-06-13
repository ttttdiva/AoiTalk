from src.services.trpg_coc import normalize_coc_state
from src.services.trpg_coc_system import (
    COC_CONDITION_DEAD,
    COC_CONDITION_INDEFINITE_INSANITY,
    COC_CONDITION_MAJOR_WOUND,
    COC_CONDITION_TEMPORARY_INSANITY,
    apply_coc_damage,
    apply_coc_checked_skill_development,
    apply_coc_sanity_loss,
    apply_coc_skill_development,
    apply_coc_spell_cost,
    calculate_coc6_damage_bonus,
    checked_coc_development_skills,
    coc6_resistance_target,
    create_coc_creature_state,
    evaluate_coc6_resistance,
    mark_coc_skill_experience,
    resolve_coc_attack,
)


def test_coc6_damage_bonus_bands():
    assert calculate_coc6_damage_bonus(6, 6) == "-1d6"
    assert calculate_coc6_damage_bonus(8, 8) == "-1d4"
    assert calculate_coc6_damage_bonus(12, 12) == "0"
    assert calculate_coc6_damage_bonus(16, 16) == "+1d4"
    assert calculate_coc6_damage_bonus(20, 20) == "+1d6"
    assert calculate_coc6_damage_bonus(30, 30) == "+3d6"


def test_coc_damage_sets_major_wound_and_death_conditions():
    state = normalize_coc_state({"hp": 10, "max_hp": 10}, "探索者", "coc6")

    damaged, event = apply_coc_damage(state, 6, "落下")
    assert event["major_wound"]
    assert damaged["hp"] == 4
    assert COC_CONDITION_MAJOR_WOUND in damaged["conditions"]

    dead, event = apply_coc_damage(damaged, 5, "追撃")
    assert event["dead"]
    assert dead["hp"] == -1
    assert COC_CONDITION_DEAD in dead["conditions"]


def test_coc_sanity_loss_tracks_temporary_and_indefinite_insanity():
    state = normalize_coc_state({"sanity": 50}, "探索者", "coc6")

    updated, event = apply_coc_sanity_loss(state, 5, "神話的恐怖")
    assert event["temporary_insanity"]
    assert updated["sanity"] == 45
    assert COC_CONDITION_TEMPORARY_INSANITY in updated["conditions"]

    updated, event = apply_coc_sanity_loss(updated, 5, "継続する恐怖")
    assert event["indefinite_insanity"]
    assert updated["sanity"] == 40
    assert COC_CONDITION_INDEFINITE_INSANITY in updated["conditions"]


def test_coc_resistance_table_uses_active_minus_passive():
    assert coc6_resistance_target(10, 10) == 50
    assert coc6_resistance_target(12, 10) == 60
    assert coc6_resistance_target(1, 30) == 5
    assert coc6_resistance_target(30, 1) == 95

    result = evaluate_coc6_resistance(total=60, active=12, passive=10)
    assert result["success"]
    assert result["target"] == 60


def test_coc_skill_experience_and_development_check():
    state = normalize_coc_state({"skills": {"目星": 55}}, "探索者", "coc6")

    marked = mark_coc_skill_experience(state, "目星")
    assert marked["skill_checks"]["目星"] is True

    updated, result = apply_coc_skill_development(marked, "目星", development_roll_total=80, gain=4)
    assert result["improved"]
    assert result["before"] == 55
    assert updated["skills"]["目星"] == 59
    assert updated["skill_checks"]["目星"] is False


def test_coc_checked_skill_development_runs_only_marked_skills():
    state = normalize_coc_state(
        {
            "skills": {"目星": 55, "聞き耳": 40, "クトゥルフ神話": 5},
            "skill_checks": {"目星": True, "聞き耳": False, "クトゥルフ神話": True},
        },
        "探索者",
        "coc6",
    )

    assert checked_coc_development_skills(state) == ["目星"]

    updated, results = apply_coc_checked_skill_development(
        state,
        development_rolls={"目星": 80},
        gains={"目星": 6},
    )

    assert len(results) == 1
    assert results[0]["skill"] == "目星"
    assert results[0]["improved"]
    assert updated["skills"]["目星"] == 61
    assert updated["skills"]["聞き耳"] == 40
    assert updated["skill_checks"]["目星"] is False
    assert updated["skill_checks"]["クトゥルフ神話"] is True


def test_coc_attack_resolves_hit_damage_and_defender_state():
    attacker = normalize_coc_state(
        {"characteristics": {"STR": 14, "SIZ": 13}, "skills": {"こぶし（パンチ）": 60}},
        "攻撃者",
        "coc6",
    )
    defender = normalize_coc_state({"hp": 10, "max_hp": 10, "skills": {"回避": 20}}, "防御者", "coc6")

    updated_defender, result = resolve_coc_attack(
        attacker,
        "こぶし",
        attack_total=30,
        defender_state=defender,
        defense_total=80,
        damage_roll={"expression": "1d3+1d4", "terms": [], "total": 5},
    )

    assert result["hit"]
    assert result["attack"]["success"]
    assert result["damage"]["result"]["amount"] == 5
    assert updated_defender["hp"] == 5


def test_coc_spell_costs_apply_multiple_resources():
    state = normalize_coc_state(
        {"hp": 10, "max_hp": 10, "mp": 8, "max_mp": 8, "sanity": 40, "characteristics": {"POW": 10}},
        "術者",
        "coc6",
    )

    updated, result = apply_coc_spell_cost(
        state,
        "門の創造",
        mp_cost=3,
        san_cost=2,
        hp_cost=1,
        pow_cost=1,
    )

    assert [event["type"] for event in result["events"]] == [
        "mp_cost",
        "sanity_loss",
        "damage",
        "pow_cost",
    ]
    assert updated["mp"] == 5
    assert updated["sanity"] == 38
    assert updated["hp"] == 9
    assert updated["characteristics"]["POW"] == 9
    assert updated["spell_casts"][0]["spell"] == "門の創造"


def test_coc_creature_state_has_runtime_fields():
    state = create_coc_creature_state(
        {
            "name": "深きもの",
            "stats": {"STR": 16, "CON": 12, "POW": 10, "DEX": 10, "APP": 1, "SIZ": 14, "INT": 11, "EDU": 1},
            "san_loss": "0/1D6",
        }
    )

    assert state["sheet_format"] == "coc_creature_v1"
    assert state["name"] == "深きもの"
    assert state["hp"] == 13
    assert state["damage_bonus"] == "+1d4"
    assert state["san_loss"] == "0/1D6"
