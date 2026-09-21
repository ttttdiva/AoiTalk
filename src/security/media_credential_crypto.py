"""Dedicated encryption helpers for Media Operations credentials.

The generic field crypto module remains backward compatible.  This module
adds a credential/account-bound AAD and explicit key-id handling so ciphertext
cannot be moved between accounts or silently decrypted with a different key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .field_crypto import FieldCryptoError, get_data_key


MEDIA_CREDENTIAL_ENCRYPTION_PREFIX = "enc:v1:"
MEDIA_CREDENTIAL_ALGORITHM = "aes256gcm"
MEDIA_CREDENTIAL_MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024
_NONCE_SIZE = 12
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class MediaCredentialCryptoError(FieldCryptoError):
    """Safe crypto error with no secret/ciphertext detail."""


class MediaCredentialKeyUnavailable(MediaCredentialCryptoError):
    """The active/recorded encryption key cannot be loaded."""


def credential_aad(credential_id: UUID | str, platform_account_id: UUID | str) -> str:
    """Return the stable dynamic AAD required by the vault contract."""

    return f"{credential_id}:{platform_account_id}"


def _key_id() -> str:
    value = str(
        os.getenv("AOITALK_MEDIA_CREDENTIAL_ACTIVE_KEY_ID")
        or os.getenv("AOITALK_MEDIA_CREDENTIAL_KEY_ID")
        or "local"
    ).strip()
    if not _KEY_ID_RE.fullmatch(value):
        raise MediaCredentialKeyUnavailable("media credential key id is invalid")
    return value


def _key_for_id(key_id: str) -> bytes:
    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        raise MediaCredentialKeyUnavailable("media credential key id is invalid")
    # A dedicated key command/env may be used for staged rotation.  The
    # existing OS-backed field key provider remains the secure fallback.
    env_name = "AOITALK_MEDIA_CREDENTIAL_KEY_B64_" + re.sub(r"[^A-Za-z0-9]", "_", key_id).upper()
    encoded = os.getenv(env_name)
    if encoded is None and key_id == _key_id():
        encoded = os.getenv("AOITALK_MEDIA_CREDENTIAL_KEY_B64")
    command_json = os.getenv("AOITALK_MEDIA_CREDENTIAL_KEY_COMMAND_JSON")
    command = os.getenv("AOITALK_MEDIA_CREDENTIAL_KEY_COMMAND")
    if command_json or command:
        try:
            from .field_crypto import _run_key_provider  # type: ignore[attr-defined]

            if command_json:
                parsed = json.loads(command_json)
                if isinstance(parsed, dict):
                    parsed = parsed.get("command")
                if isinstance(parsed, str):
                    argv = shlex.split(parsed, posix=os.name != "nt")
                elif isinstance(parsed, list) and all(isinstance(item, str) and item for item in parsed):
                    argv = list(parsed)
                else:
                    raise ValueError
                # The key-id is an explicit non-secret selector.  Providers
                # can map it to a KMS/keyring version without exposing key
                # material to this process's arguments or logs.
                argv.append(key_id)
            else:
                argv = shlex.split(command, posix=os.name != "nt")
                # Plain command configuration supports either an explicit
                # ``{key_id}``/``$KEY_ID`` placeholder or the conventional
                # trailing selector argument.  The latter keeps versioned
                # providers usable while still allowing a single-key command
                # to ignore an extra argument when it is implemented as a
                # wrapper script.
                placeholders = {"{key_id}", "{KEY_ID}", "$KEY_ID", "%KEY_ID%"}
                if any(token in placeholders for token in argv):
                    argv = [
                        key_id if token in placeholders else token
                        for token in argv
                    ]
                else:
                    argv.append(key_id)
            if not argv:
                raise ValueError
            key = _run_key_provider(argv, shell=False, provider="media credential key command")
        except Exception:
            raise MediaCredentialKeyUnavailable("media credential key is unavailable") from None
    elif encoded:
        if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "").lower() not in {"1", "true", "yes"}:
            raise MediaCredentialKeyUnavailable("media credential environment keys are disabled")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise MediaCredentialKeyUnavailable("media credential key is invalid") from None
    elif key_id == "local":
        try:
            key = get_data_key()
        except Exception as exc:
            raise MediaCredentialKeyUnavailable("media credential key is unavailable") from None
    else:
        # If a rotation key is configured via a command, field_crypto's
        # provider command is intentionally reused.  This keeps production
        # key material outside process arguments and the database.
        raise MediaCredentialKeyUnavailable("media credential key is unavailable")
    if len(key) != 32:
        raise MediaCredentialKeyUnavailable("media credential key is invalid")
    return key


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) % 4 == 1:
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise MediaCredentialCryptoError("encrypted media credential format is invalid") from None
    return raw


def media_credential_ciphertext_key_id(ciphertext: str) -> str:
    """Extract and validate the key id embedded in a vault ciphertext."""

    if not isinstance(ciphertext, str):
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    parts = ciphertext.split(":", 4)
    if len(parts) != 5 or parts[:3] != ["enc", "v1", MEDIA_CREDENTIAL_ALGORITHM] or not _KEY_ID_RE.fullmatch(parts[3]):
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    return parts[3]


def canonical_payload(payload: Any) -> bytes:
    try:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        raw = rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError, MemoryError):
        raise MediaCredentialCryptoError("media credential payload is not serializable") from None
    if len(raw) == 0 or len(raw) > MEDIA_CREDENTIAL_MAX_PLAINTEXT_BYTES:
        raise MediaCredentialCryptoError("media credential payload exceeds maximum size")
    return raw


@dataclass(frozen=True)
class EncryptedMediaCredential:
    ciphertext: str
    key_id: str
    payload_digest: str


def encrypt_media_credential(
    payload: Any,
    *,
    credential_id: UUID | str,
    platform_account_id: UUID | str,
) -> EncryptedMediaCredential:
    raw = canonical_payload(payload)
    key_id = _key_id()
    try:
        key = _key_for_id(key_id)
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(key).encrypt(nonce, raw, credential_aad(credential_id, platform_account_id).encode("utf-8"))
    except MediaCredentialCryptoError:
        raise
    except Exception:
        raise MediaCredentialCryptoError("media credential encryption failed") from None
    return EncryptedMediaCredential(
        ciphertext=":".join(("enc", "v1", MEDIA_CREDENTIAL_ALGORITHM, key_id, _encode(nonce), _encode(ciphertext))),
        key_id=key_id,
        payload_digest=hashlib.sha256(raw).hexdigest(),
    )


def decrypt_media_credential(
    ciphertext: str,
    *,
    credential_id: UUID | str,
    platform_account_id: UUID | str,
) -> Any:
    if not isinstance(ciphertext, str):
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    parts = ciphertext.split(":")
    if len(parts) != 6 or parts[:3] != ["enc", "v1", MEDIA_CREDENTIAL_ALGORITHM]:
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    key_id = parts[3]
    nonce = _decode(parts[4])
    encrypted = _decode(parts[5])
    if len(nonce) != _NONCE_SIZE or len(encrypted) < 16 or len(encrypted) > MEDIA_CREDENTIAL_MAX_PLAINTEXT_BYTES + 16:
        raise MediaCredentialCryptoError("encrypted media credential format is invalid")
    try:
        raw = AESGCM(_key_for_id(key_id)).decrypt(
            nonce,
            encrypted,
            credential_aad(credential_id, platform_account_id).encode("utf-8"),
        )
    except MediaCredentialKeyUnavailable:
        raise
    except InvalidTag:
        raise MediaCredentialCryptoError("encrypted media credential authentication failed") from None
    except Exception:
        raise MediaCredentialCryptoError("media credential decryption failed") from None
    if len(raw) > MEDIA_CREDENTIAL_MAX_PLAINTEXT_BYTES:
        raise MediaCredentialCryptoError("media credential payload exceeds maximum size")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError):
        raise MediaCredentialCryptoError("encrypted media credential payload is invalid") from None


def decrypt_media_credential_safe(*args: Any, **kwargs: Any) -> Any:
    """Compatibility wrapper returning ``None`` when key material is absent."""

    try:
        return decrypt_media_credential(*args, **kwargs)
    except MediaCredentialKeyUnavailable:
        return None


__all__ = [
    "EncryptedMediaCredential",
    "MEDIA_CREDENTIAL_ALGORITHM",
    "MEDIA_CREDENTIAL_ENCRYPTION_PREFIX",
    "MEDIA_CREDENTIAL_MAX_PLAINTEXT_BYTES",
    "MediaCredentialCryptoError",
    "MediaCredentialKeyUnavailable",
    "media_credential_ciphertext_key_id",
    "canonical_payload",
    "credential_aad",
    "decrypt_media_credential",
    "decrypt_media_credential_safe",
    "encrypt_media_credential",
]
