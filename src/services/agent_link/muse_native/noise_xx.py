"""Small Noise_XX_25519_AESGCM_SHA256 implementation using cryptography."""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..codes import AgentLinkFailure, LinkErrorCode

_PROTOCOL_NAME = b"Noise_XX_25519_AESGCM_SHA256"
_MAX_NONCE = 2**64 - 1


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _hkdf2(chaining_key: bytes, input_key_material: bytes) -> tuple[bytes, bytes]:
    temporary_key = hmac.new(chaining_key, input_key_material, hashlib.sha256).digest()
    first = hmac.new(temporary_key, b"\x01", hashlib.sha256).digest()
    second = hmac.new(temporary_key, first + b"\x02", hashlib.sha256).digest()
    return first, second


@dataclass
class CipherState:
    """Directional Noise cipher state. Key material is excluded from repr."""

    _key: bytes = field(repr=False)
    _nonce: int = field(default=0, repr=False)

    def __repr__(self) -> str:
        return "CipherState(<redacted>)"

    @property
    def nonce(self) -> int:
        return self._nonce

    def _nonce_bytes(self) -> bytes:
        if self._nonce > _MAX_NONCE:
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        return b"\x00\x00\x00\x00" + struct.pack(">Q", self._nonce)

    def encrypt(self, plaintext: bytes, associated_data: bytes = b"") -> bytes:
        nonce = self._nonce_bytes()
        try:
            ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, associated_data)
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT) from None
        self._nonce += 1
        return ciphertext

    def decrypt(self, ciphertext: bytes, associated_data: bytes = b"") -> bytes:
        nonce = self._nonce_bytes()
        try:
            plaintext = AESGCM(self._key).decrypt(
                nonce, ciphertext, associated_data
            )
        except (InvalidTag, ValueError, TypeError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT) from None
        self._nonce += 1
        return plaintext


class _SymmetricState:
    __slots__ = ("chaining_key", "handshake_hash", "key", "nonce")

    def __init__(self) -> None:
        if len(_PROTOCOL_NAME) <= 32:
            initial = _PROTOCOL_NAME.ljust(32, b"\x00")
        else:
            initial = hashlib.sha256(_PROTOCOL_NAME).digest()
        self.chaining_key = initial
        self.handshake_hash = initial
        self.key: bytes | None = None
        self.nonce = 0
        self.mix_hash(b"")

    def mix_hash(self, data: bytes) -> None:
        self.handshake_hash = hashlib.sha256(self.handshake_hash + data).digest()

    def mix_key(self, input_key_material: bytes) -> None:
        self.chaining_key, self.key = _hkdf2(
            self.chaining_key, input_key_material
        )
        self.nonce = 0

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        if self.key is None:
            ciphertext = plaintext
        else:
            if self.nonce > _MAX_NONCE:
                raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
            nonce = b"\x00\x00\x00\x00" + struct.pack(">Q", self.nonce)
            ciphertext = AESGCM(self.key).encrypt(
                nonce, plaintext, self.handshake_hash
            )
            self.nonce += 1
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        if self.key is None:
            plaintext = ciphertext
        else:
            if self.nonce > _MAX_NONCE:
                raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
            nonce = b"\x00\x00\x00\x00" + struct.pack(">Q", self.nonce)
            try:
                plaintext = AESGCM(self.key).decrypt(
                    nonce, ciphertext, self.handshake_hash
                )
            except (InvalidTag, ValueError, TypeError):
                raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None
            self.nonce += 1
        self.mix_hash(ciphertext)
        return plaintext

    def split(self, *, initiator: bool) -> tuple[CipherState, CipherState]:
        first, second = _hkdf2(self.chaining_key, b"")
        if initiator:
            return CipherState(first), CipherState(second)
        return CipherState(second), CipherState(first)


def _dh(private_key: X25519PrivateKey, public_bytes: bytes) -> bytes:
    try:
        public_bytes = bytes(public_bytes)
    except (TypeError, ValueError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None
    if len(public_bytes) != 32:
        raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
    try:
        return private_key.exchange(X25519PublicKey.from_public_bytes(public_bytes))
    except (ValueError, TypeError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None


class NoiseXXInitiator:
    """Initiator for the standard three-message, empty-payload XX handshake."""

    def __init__(self) -> None:
        self._symmetric = _SymmetricState()
        self._ephemeral = X25519PrivateKey.generate()
        self._static = X25519PrivateKey.generate()
        self._remote_ephemeral: bytes | None = None
        self._remote_static: bytes | None = None
        self._started = False
        self._finished = False
        self.send_cipher: CipherState | None = None
        self.receive_cipher: CipherState | None = None

    def __repr__(self) -> str:
        return "NoiseXXInitiator(<key material redacted>)"

    def start(self) -> bytes:
        if self._started:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        self._started = True
        ephemeral = _raw_public(self._ephemeral.public_key())
        self._symmetric.mix_hash(ephemeral)
        # Noise appends EncryptAndHash(payload) after each message pattern,
        # including message 1's empty payload.
        self._symmetric.encrypt_and_hash(b"")
        return ephemeral

    def receive_message2(self, message: bytes) -> bytes:
        try:
            message = bytes(message)
        except (TypeError, ValueError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None
        if not self._started or self._remote_ephemeral is not None or len(message) < 32:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        remote_ephemeral = message[:32]
        self._remote_ephemeral = remote_ephemeral
        self._symmetric.mix_hash(remote_ephemeral)
        self._symmetric.mix_key(_dh(self._ephemeral, remote_ephemeral))

        encrypted_static = message[32:80]
        remote_static = self._symmetric.decrypt_and_hash(encrypted_static)
        if len(remote_static) != 32:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        self._remote_static = remote_static
        self._symmetric.mix_key(_dh(self._ephemeral, remote_static))
        payload = self._symmetric.decrypt_and_hash(message[80:])
        return payload

    def finish(self, payload: bytes = b"") -> bytes:
        if self._remote_ephemeral is None or self._remote_static is None or self._finished:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        static_public = _raw_public(self._static.public_key())
        encrypted_static = self._symmetric.encrypt_and_hash(static_public)
        self._symmetric.mix_key(_dh(self._static, self._remote_ephemeral))
        encrypted_payload = self._symmetric.encrypt_and_hash(payload)
        self.send_cipher, self.receive_cipher = self._symmetric.split(initiator=True)
        self._finished = True
        return encrypted_static + encrypted_payload


class NoiseXXResponder:
    """Responder used for interoperability and local gateway tests."""

    def __init__(self) -> None:
        self._symmetric = _SymmetricState()
        self._ephemeral: X25519PrivateKey | None = None
        self._static = X25519PrivateKey.generate()
        self._remote_ephemeral: bytes | None = None
        self._remote_static: bytes | None = None
        self._finished = False
        self.send_cipher: CipherState | None = None
        self.receive_cipher: CipherState | None = None

    def __repr__(self) -> str:
        return "NoiseXXResponder(<key material redacted>)"

    def receive_message1(self, message: bytes) -> None:
        try:
            message = bytes(message)
        except (TypeError, ValueError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None
        if self._remote_ephemeral is not None or len(message) != 32:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        self._remote_ephemeral = bytes(message)
        self._symmetric.mix_hash(message)
        self._symmetric.decrypt_and_hash(b"")

    def message2(self, payload: bytes = b"") -> bytes:
        if self._remote_ephemeral is None or self._ephemeral is not None:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        self._ephemeral = X25519PrivateKey.generate()
        ephemeral = _raw_public(self._ephemeral.public_key())
        self._symmetric.mix_hash(ephemeral)
        self._symmetric.mix_key(_dh(self._ephemeral, self._remote_ephemeral))
        encrypted_static = self._symmetric.encrypt_and_hash(
            _raw_public(self._static.public_key())
        )
        self._symmetric.mix_key(_dh(self._static, self._remote_ephemeral))
        encrypted_payload = self._symmetric.encrypt_and_hash(payload)
        return ephemeral + encrypted_static + encrypted_payload

    def receive_message3(self, message: bytes) -> bytes:
        try:
            message = bytes(message)
        except (TypeError, ValueError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None
        if self._ephemeral is None or self._finished or len(message) < 48:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        remote_static = self._symmetric.decrypt_and_hash(message[:48])
        if len(remote_static) != 32:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
        self._remote_static = remote_static
        self._symmetric.mix_key(_dh(self._ephemeral, remote_static))
        payload = self._symmetric.decrypt_and_hash(message[48:])
        self.send_cipher, self.receive_cipher = self._symmetric.split(initiator=False)
        self._finished = True
        return payload
