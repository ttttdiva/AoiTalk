"""Identity-bound integration encryption, following the hardened Media vault.

The key namespace and AAD are deliberately separate from Media. Versioned
key commands receive only a non-secret key selector, never credential data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .field_crypto import FieldCryptoError, get_data_key


MAX_CREDENTIAL_BYTES = 64 * 1024
_KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class IntegrationCredentialCryptoError(FieldCryptoError):
    """Only bounded, secret-free error messages may cross this boundary."""


class IntegrationCredentialKeyUnavailable(IntegrationCredentialCryptoError):
    pass


class IntegrationCredentialKeyProvider(Protocol):
    def active_key_id(self) -> str: ...
    def key_for_id(self, key_id: str) -> bytes: ...


class EnvironmentIntegrationCredentialKeyProvider:
    def active_key_id(self) -> str:
        value = os.getenv("AOITALK_INTEGRATION_CREDENTIAL_ACTIVE_KEY_ID", "local")
        if not _KEY_ID.fullmatch(value):
            raise IntegrationCredentialKeyUnavailable("integration_key_unavailable")
        return value

    def key_for_id(self, key_id: str) -> bytes:
        try:
            if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                raise ValueError
            command_json = os.getenv("AOITALK_INTEGRATION_CREDENTIAL_KEY_COMMAND_JSON")
            command = os.getenv("AOITALK_INTEGRATION_CREDENTIAL_KEY_COMMAND")
            suffix = re.sub(r"[^A-Za-z0-9]", "_", key_id).upper()
            encoded = os.getenv("AOITALK_INTEGRATION_CREDENTIAL_KEY_B64_" + suffix)
            if encoded is None and key_id == self.active_key_id():
                encoded = os.getenv("AOITALK_INTEGRATION_CREDENTIAL_KEY_B64")
            if command_json or command:
                from .field_crypto import _run_key_provider

                argv = json.loads(command_json) if command_json else shlex.split(command, posix=os.name != "nt")
                if isinstance(argv, dict):
                    argv = argv.get("command")
                if isinstance(argv, str):
                    argv = shlex.split(argv, posix=os.name != "nt")
                if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
                    raise ValueError
                placeholders = {"{key_id}", "{KEY_ID}", "$KEY_ID", "%KEY_ID%"}
                if any(x in placeholders for x in argv):
                    argv = [key_id if x in placeholders else x for x in argv]
                else:
                    argv = [*argv, key_id]
                key = _run_key_provider(argv, shell=False, provider="integration credential key command")
            elif encoded:
                if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "").lower() not in {"true", "1", "yes"}:
                    raise ValueError
                key = base64.b64decode(encoded, validate=True)
            elif key_id == "local":
                key = get_data_key()
            else:
                raise ValueError
            if type(key) is not bytes or len(key) != 32:
                raise ValueError
            return key
        except Exception:
            raise IntegrationCredentialKeyUnavailable("integration_key_unavailable") from None


def _key(provider: IntegrationCredentialKeyProvider, key_id: str) -> bytes:
    try:
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ValueError
        value = provider.key_for_id(key_id)
        if type(value) is not bytes or len(value) != 32:
            raise ValueError
        return value
    except Exception:
        raise IntegrationCredentialKeyUnavailable("integration_key_unavailable") from None


def credential_aad(credential_id: UUID | str, connection_id: UUID | str) -> bytes:
    try:
        return f"integration:v1:{UUID(str(credential_id))}:{UUID(str(connection_id))}".encode("ascii")
    except Exception:
        raise IntegrationCredentialCryptoError("integration_identity_invalid") from None


def canonical_payload(payload: Any) -> bytes:
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not raw or len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValueError
        return raw
    except Exception:
        raise IntegrationCredentialCryptoError("integration_payload_invalid") from None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) % 4 == 1:
        raise ValueError
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def integration_credential_ciphertext_key_id(ciphertext: str) -> str:
    try:
        if not isinstance(ciphertext, str) or len(ciphertext) > MAX_CREDENTIAL_BYTES * 2:
            raise ValueError
        parts = ciphertext.split(":")
        if len(parts) != 6 or parts[:3] != ["enc", "v1", "aes256gcm"] or not _KEY_ID.fullmatch(parts[3]):
            raise ValueError
        return parts[3]
    except Exception:
        raise IntegrationCredentialCryptoError("integration_ciphertext_invalid") from None


@dataclass(frozen=True)
class EncryptedIntegrationCredential:
    ciphertext: str = field(repr=False)
    key_id: str
    payload_digest: str = field(repr=False)


def encrypt_integration_credential(payload: Any, *, credential_id: UUID | str, connection_id: UUID | str,
                                   key_provider: IntegrationCredentialKeyProvider | None = None) -> EncryptedIntegrationCredential:
    provider = key_provider or EnvironmentIntegrationCredentialKeyProvider()
    raw = canonical_payload(payload)
    try:
        key_id = provider.active_key_id()
    except Exception:
        raise IntegrationCredentialKeyUnavailable("integration_key_unavailable") from None
    key = _key(provider, key_id)
    aad = credential_aad(credential_id, connection_id)
    try:
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, raw, aad)
        return EncryptedIntegrationCredential(
            f"enc:v1:aes256gcm:{key_id}:{_encode(nonce)}:{_encode(encrypted)}",
            key_id, hashlib.sha256(raw).hexdigest(),
        )
    except Exception:
        raise IntegrationCredentialCryptoError("integration_encryption_failed") from None


def decrypt_integration_credential(ciphertext: str, *, credential_id: UUID | str, connection_id: UUID | str,
                                   key_provider: IntegrationCredentialKeyProvider | None = None) -> Any:
    key_id = integration_credential_ciphertext_key_id(ciphertext)
    provider = key_provider or EnvironmentIntegrationCredentialKeyProvider()
    key = _key(provider, key_id)
    aad = credential_aad(credential_id, connection_id)
    try:
        parts = ciphertext.split(":")
        nonce, encrypted = _decode(parts[4]), _decode(parts[5])
        if len(nonce) != 12 or not 16 <= len(encrypted) <= MAX_CREDENTIAL_BYTES + 16:
            raise ValueError
        raw = AESGCM(key).decrypt(nonce, encrypted, aad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise IntegrationCredentialCryptoError("integration_ciphertext_invalid") from None
