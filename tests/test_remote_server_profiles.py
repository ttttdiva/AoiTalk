from datetime import datetime

from src.memory.models.remote import RemoteServerProfile


def test_remote_server_profile_columns():
    columns = {c.name for c in RemoteServerProfile.__table__.columns}
    expected = {
        "id",
        "user_id",
        "name",
        "base_url",
        "auth_token",
        "display_color",
        "enabled",
        "last_status",
        "last_checked_at",
        "last_capabilities",
        "created_at",
        "updated_at",
    }
    assert expected <= columns


def test_remote_server_profile_unique_constraint():
    constraint_names = {
        c.name for c in RemoteServerProfile.__table__.constraints if c.name
    }
    assert "uq_remote_server_profiles_user_base_url" in constraint_names


def test_to_dict_excludes_token_by_default():
    profile = RemoteServerProfile.__new__(RemoteServerProfile)
    profile.id = "11111111-1111-1111-1111-111111111111"
    profile.user_id = "22222222-2222-2222-2222-222222222222"
    profile.name = "会社版"
    profile.base_url = "https://company.example.com"
    profile.auth_token = "enc:v1:aes256gcm:local:dummy"
    profile.display_color = "#ff8800"
    profile.enabled = True
    profile.last_status = "ok"
    profile.last_checked_at = datetime(2026, 6, 12, 10, 0, 0)
    profile.last_capabilities = {"version": "1.0"}
    profile.created_at = datetime(2026, 6, 12, 9, 0, 0)
    profile.updated_at = datetime(2026, 6, 12, 9, 30, 0)

    data = profile.to_dict()
    assert "auth_token" not in data
    assert data["has_token"] is True
    assert data["name"] == "会社版"
    assert data["base_url"] == "https://company.example.com"
    assert data["last_capabilities"] == {"version": "1.0"}
    assert data["last_checked_at"] == "2026-06-12T10:00:00"


def test_to_dict_has_token_false_when_empty():
    profile = RemoteServerProfile.__new__(RemoteServerProfile)
    profile.id = "11111111-1111-1111-1111-111111111111"
    profile.user_id = "22222222-2222-2222-2222-222222222222"
    profile.name = "テスト"
    profile.base_url = "https://example.com"
    profile.auth_token = None
    profile.display_color = None
    profile.enabled = True
    profile.last_status = None
    profile.last_checked_at = None
    profile.last_capabilities = None
    profile.created_at = None
    profile.updated_at = None

    data = profile.to_dict()
    assert data["has_token"] is False
    assert data["last_checked_at"] is None
    assert data["created_at"] is None


def test_token_aad_binds_user_id():
    profile = RemoteServerProfile.__new__(RemoteServerProfile)
    profile.user_id = "22222222-2222-2222-2222-222222222222"
    aad = profile._token_aad()
    assert aad == (
        "remote_server_profiles.auth_token:22222222-2222-2222-2222-222222222222"
    )
