import base64
import os

from src.security import field_crypto


def setup_function():
    field_crypto.get_data_key.cache_clear()


def test_encrypt_decrypt_text_with_explicit_test_key(monkeypatch):
    key = base64.b64encode(b"0" * 32).decode("ascii")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "true")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_KEY_B64", key)

    encrypted = field_crypto.encrypt_text("secret text", aad="test.field")

    assert encrypted.startswith("enc:v1:")
    assert encrypted != "secret text"
    assert field_crypto.decrypt_text(encrypted, aad="test.field") == "secret text"
    assert field_crypto.decrypt_text_if_needed("legacy plaintext", aad="test.field") == "legacy plaintext"


def test_encrypt_json_secret_leaves_only_encrypts_sensitive_keys(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "true")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_KEY_B64", key)

    payload = {
        "openrouter_api_key": "sk-test",
        "display_name": "not secret",
        "nested": {"client_secret": "client-secret"},
    }

    encrypted = field_crypto.encrypt_json_secret_leaves(payload, aad_prefix="config")

    assert encrypted["openrouter_api_key"].startswith("enc:v1:")
    assert encrypted["nested"]["client_secret"].startswith("enc:v1:")
    assert encrypted["display_name"] == "not secret"
    assert field_crypto.decrypt_json_secret_leaves(encrypted, aad_prefix="config") == payload


def test_encrypt_json_value_round_trip(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "true")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_KEY_B64", key)

    payload = {"request_text": "顧客確認事項", "nested": {"count": 3}}

    encrypted = field_crypto.encrypt_json_value(payload, aad="record_rows.values")

    assert isinstance(encrypted, str)
    assert encrypted.startswith("enc:v1:")
    assert field_crypto.decrypt_json_value_if_needed(encrypted, aad="record_rows.values") == payload


def test_model_encrypted_properties_store_ciphertext(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "true")
    monkeypatch.setenv("AOITALK_FIELD_CRYPTO_KEY_B64", key)
    field_crypto.get_data_key.cache_clear()

    from src.memory.models import ConversationMessage, RecordRow

    message = ConversationMessage(role="user", content="hello")
    assert message._content.startswith("enc:v1:")
    assert message.content == "hello"

    row = RecordRow(values={"request_text": "確認してください"}, title="確認事項")
    assert isinstance(row._values, str)
    assert row._values.startswith("enc:v1:")
    assert row._title.startswith("enc:v1:")
    assert row.values == {"request_text": "確認してください"}
    assert row.title == "確認事項"
