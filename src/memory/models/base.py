"""SQLAlchemy declarative base と暗号化フィールド用ヘルパー。

全モデルモジュールはこの ``Base`` を共有する（metadata の重複登録を防ぐ）。
"""

from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declarative_base

from ...security.field_crypto import (
    decrypt_json_value_if_needed,
    decrypt_text_if_needed,
    encrypt_json_value,
    encrypt_text,
)

# pgvector removed - using Qdrant for vector search instead


class _DeclarativeModel:
    """Provide SQLAlchemy state even when tests instantiate with __new__ only."""

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        manager = getattr(cls, "_sa_class_manager", None)
        if (
            manager is not None
            and getattr(instance, "_sa_instance_state", None) is None
        ):
            manager.setup_instance(instance)
        return instance


Base = declarative_base(cls=_DeclarativeModel)


def _encrypted_text_property(storage_attr: str, aad: str):
    def getter(self):
        return decrypt_text_if_needed(getattr(self, storage_attr), aad=aad)

    def setter(self, value):
        setattr(self, storage_attr, encrypt_text(value, aad=aad))

    def expression(cls):
        return getattr(cls, storage_attr)

    return hybrid_property(getter, setter, expr=expression)


def _encrypted_json_property(storage_attr: str, aad: str):
    def getter(self):
        return decrypt_json_value_if_needed(getattr(self, storage_attr), aad=aad)

    def setter(self, value):
        setattr(self, storage_attr, encrypt_json_value(value, aad=aad))

    def expression(cls):
        return getattr(cls, storage_attr)

    return hybrid_property(getter, setter, expr=expression)
