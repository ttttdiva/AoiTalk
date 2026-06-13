import base64
import os
from uuid import uuid4


def _enable_env_key():
    os.environ["AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY"] = "true"
    os.environ["AOITALK_FIELD_CRYPTO_KEY_B64"] = base64.b64encode(
        b"0" * 32
    ).decode("ascii")
    from src.security import field_crypto

    field_crypto.get_data_key.cache_clear()


def test_normalize_base_url_strips_trailing_slash():
    from src.memory.remote_server_repository import _normalize_base_url

    assert _normalize_base_url("https://x.example.com/") == "https://x.example.com"
    assert _normalize_base_url("  https://x.example.com  ") == "https://x.example.com"


def test_encrypt_token_none_for_empty():
    from src.memory.remote_server_repository import RemoteServerRepository

    assert RemoteServerRepository._encrypt_token(uuid4(), None) is None
    assert RemoteServerRepository._encrypt_token(uuid4(), "") is None


def test_encrypt_token_roundtrip_with_user_aad():
    _enable_env_key()
    from src.memory.remote_server_repository import RemoteServerRepository
    from src.security.field_crypto import decrypt_text

    user_id = uuid4()
    ciphertext = RemoteServerRepository._encrypt_token(user_id, "secret-token")
    assert ciphertext is not None
    assert ciphertext.startswith("enc:v1:")

    aad = f"remote_server_profiles.auth_token:{user_id}"
    assert decrypt_text(ciphertext, aad=aad) == "secret-token"
