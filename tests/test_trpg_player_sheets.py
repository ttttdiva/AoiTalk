import uuid

from src.models.ecc_models import TRPGPlayerCharacterSheet


def test_trpg_player_character_sheet_dict_uses_dedicated_source():
    sheet = TRPGPlayerCharacterSheet(
        id=uuid.uuid4(),
        scenario_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        ruleset="coc6",
        name="探索者A",
        description="プレイヤーキャラクター",
        trpg_pc_state={"hp": 12, "skills": {"目星": 60}},
    )

    payload = sheet.to_dict()

    assert payload["sheet_source"] == "trpg_player_character_sheets"
    assert "role" not in payload
    assert payload["trpg_ruleset"] == "coc6"
    assert payload["trpg_pc_state"]["skills"]["目星"] == 60
