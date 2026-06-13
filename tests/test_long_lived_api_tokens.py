from __future__ import annotations

import inspect
import uuid
from datetime import datetime


def test_long_lived_api_token_model_columns() -> None:
    from src.memory.models import LongLivedApiToken

    columns = {column.name for column in LongLivedApiToken.__table__.columns}
    assert columns == {
        "id",
        "user_id",
        "name",
        "token_hash",
        "token_prefix",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked",
    }


def test_long_lived_api_token_hash_is_unique_constrained() -> None:
    from src.memory.models import LongLivedApiToken

    constraints = [
        constraint
        for constraint in LongLivedApiToken.__table__.constraints
        if getattr(constraint, "name", None)
        == "uq_long_lived_api_tokens_token_hash"
    ]
    assert len(constraints) == 1
    assert {c.name for c in constraints[0].columns} == {"token_hash"}


def test_long_lived_api_token_to_dict_excludes_hash() -> None:
    from src.memory.models import LongLivedApiToken

    token_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime(2026, 6, 12, 12, 0, 0)

    token = LongLivedApiToken.__new__(LongLivedApiToken)
    token.id = token_id
    token.user_id = user_id
    token.name = "会社サーバー連携"
    token.token_hash = "deadbeef" * 8
    token.token_prefix = "aoitpat_abc123"
    token.created_at = now
    token.last_used_at = None
    token.expires_at = None
    token.revoked = False

    result = token.to_dict()

    assert result == {
        "id": str(token_id),
        "user_id": str(user_id),
        "name": "会社サーバー連携",
        "token_prefix": "aoitpat_abc123",
        "created_at": "2026-06-12T12:00:00",
        "last_used_at": None,
        "expires_at": None,
        "revoked": False,
    }
    assert "token_hash" not in result


def test_api_token_repository_methods_are_async() -> None:
    from src.memory.api_token_repository import ApiTokenRepository

    for method_name in [
        "create_token",
        "list_tokens",
        "get_token",
        "revoke_token",
        "verify_token",
    ]:
        assert inspect.iscoroutinefunction(
            getattr(ApiTokenRepository, method_name)
        )


def test_verify_token_sync_is_not_coroutine() -> None:
    from src.memory.api_token_repository import ApiTokenRepository

    assert not inspect.iscoroutinefunction(ApiTokenRepository.verify_token_sync)
    assert callable(ApiTokenRepository.verify_token_sync)


def test_generate_token_has_prefix_and_is_unique() -> None:
    from src.memory.api_token_repository import TOKEN_PREFIX, ApiTokenRepository

    tokens = {ApiTokenRepository.generate_token() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert token.startswith(TOKEN_PREFIX)
        assert len(token) > len(TOKEN_PREFIX) + 16


def test_hash_token_is_deterministic_sha256() -> None:
    import hashlib

    from src.memory.api_token_repository import ApiTokenRepository

    token = "aoitpat_example-token-value"
    digest = ApiTokenRepository.hash_token(token)
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert ApiTokenRepository.hash_token(token) == digest
    assert digest != ApiTokenRepository.hash_token(token + "x")
