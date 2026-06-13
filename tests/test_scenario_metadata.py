from src.services.scenario_service import normalize_scenario_metadata


def test_writing_scenario_keeps_trpg_fields_empty_by_default():
    payload = normalize_scenario_metadata(
        {
            "title": "F01_Unferat 本編",
            "genre": "drama",
            "tags": ["screenplay"],
        }
    )

    assert payload["scenario_kind"] == "writing"
    assert payload["ruleset"] == ""
    assert "trpg" not in {tag.lower() for tag in payload["tags"]}


def test_trpg_scenario_marks_ruleset_and_tags():
    payload = normalize_scenario_metadata(
        {
            "title": "毒入りスープ",
            "scenario_kind": "trpg",
            "ruleset": "coc",
            "genre": "horror",
            "tags": ["毒入りスープ"],
        }
    )

    assert payload["scenario_kind"] == "trpg"
    assert payload["ruleset"] == "coc6"
    assert {"trpg", "coc6"} <= {tag.lower() for tag in payload["tags"]}
