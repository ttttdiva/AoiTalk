from src.services.trpg_gm_service import _auto_bgm_enabled, _strip_markers


def test_trpg_auto_bgm_defaults_enabled_for_existing_rooms():
    assert _auto_bgm_enabled(None) is True
    assert _auto_bgm_enabled({}) is True


def test_trpg_auto_bgm_respects_shared_state_toggle():
    assert _auto_bgm_enabled({"bgm_auto_enabled": True}) is True
    assert _auto_bgm_enabled({"bgm_auto_enabled": False}) is False


def test_strip_markers_hides_bgm_marker_from_visible_narration():
    visible = _strip_markers("扉の奥から冷たい風が吹く。[BGM:mysterious]")

    assert visible == "扉の奥から冷たい風が吹く。"
