"""Application-layer field encryption for DB-backed sensitive data.

The database stores versioned ciphertext. AoiTalk loads the data key from an
OS-protected provider and decrypts only inside the application process.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTION_PREFIX = "enc:v1:"
_ALG = "aes256gcm"
_KEY_ID = "local"
_NONCE_LEN = 12
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential|client[_-]?secret|pass)",
    re.IGNORECASE,
)


class FieldCryptoError(RuntimeError):
    """Raised when field encryption or decryption fails."""


def is_encrypted_value(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTION_PREFIX)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_key_command(command: str) -> bytes:
    completed = subprocess.run(
        command,
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip().splitlines()[-1].strip()
    return base64.b64decode(raw)


def _run_windows_dpapi_helper() -> bytes:
    script = _repo_root() / "scripts" / "field_crypto_key.ps1"
    if not script.exists():
        raise FieldCryptoError(f"field crypto key helper not found: {script}")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Action",
            "GetOrCreateDataKey",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip().splitlines()[-1].strip()
    return base64.b64decode(raw)


def _local_key_file() -> bytes:
    if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise FieldCryptoError(
            "No OS key provider is configured. On Linux/macOS set "
            "AOITALK_FIELD_CRYPTO_KEY_COMMAND to a keyring/KMS command, or "
            "explicitly allow the local key-file fallback."
        )
    key_path = Path(
        os.getenv(
            "AOITALK_FIELD_CRYPTO_LOCAL_KEY_FILE",
            str(Path.home() / ".config" / "aoitalk" / "field-crypto.key"),
        )
    )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        return base64.b64decode(key_path.read_text(encoding="ascii").strip())
    key = os.urandom(32)
    key_path.write_text(base64.b64encode(key).decode("ascii"), encoding="ascii")
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


@lru_cache(maxsize=1)
def get_data_key() -> bytes:
    """Return the 256-bit local data key.

    Environment-provided key material is intentionally disabled by default.
    It is only accepted when AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY=true for tests.
    """

    env_key = os.getenv("AOITALK_FIELD_CRYPTO_KEY_B64")
    if env_key:
        if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "").lower() not in {
            "1",
            "true",
            "yes",
        }:
            raise FieldCryptoError(
                "AOITALK_FIELD_CRYPTO_KEY_B64 is set but env keys are disabled"
            )
        key = base64.b64decode(env_key)
    elif os.getenv("AOITALK_FIELD_CRYPTO_KEY_COMMAND"):
        key = _run_key_command(os.environ["AOITALK_FIELD_CRYPTO_KEY_COMMAND"])
    elif sys.platform.startswith("win"):
        key = _run_windows_dpapi_helper()
    else:
        key = _local_key_file()

    if len(key) != 32:
        raise FieldCryptoError("field crypto data key must be 32 bytes")
    return key


def _aad_bytes(aad: Optional[str]) -> bytes:
    return (aad or "aoitalk:field:v1").encode("utf-8")


def encrypt_text(value: Optional[str], *, aad: Optional[str] = None) -> Optional[str]:
    if value is None or value == "" or is_encrypted_value(value):
        return value
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(get_data_key()).encrypt(
        nonce,
        value.encode("utf-8"),
        _aad_bytes(aad),
    )
    return ":".join(
        [
            "enc",
            "v1",
            _ALG,
            _KEY_ID,
            _b64url_encode(nonce),
            _b64url_encode(ciphertext),
        ]
    )


def decrypt_text(value: Optional[str], *, aad: Optional[str] = None) -> Optional[str]:
    if value is None or value == "":
        return value
    if not is_encrypted_value(value):
        return value
    parts = value.split(":")
    if len(parts) != 6 or parts[0] != "enc" or parts[1] != "v1" or parts[2] != _ALG:
        raise FieldCryptoError("unsupported encrypted field format")
    nonce = _b64url_decode(parts[4])
    ciphertext = _b64url_decode(parts[5])
    plaintext = AESGCM(get_data_key()).decrypt(nonce, ciphertext, _aad_bytes(aad))
    return plaintext.decode("utf-8")


def decrypt_text_if_needed(value: Optional[str], *, aad: Optional[str] = None) -> Optional[str]:
    return decrypt_text(value, aad=aad)


def encrypt_json_value(value: Any, *, aad: Optional[str] = None) -> Any:
    if value is None or is_encrypted_value(value):
        return value
    if value in ({}, []):
        return value
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return encrypt_text(serialized, aad=aad)


def decrypt_json_value_if_needed(value: Any, *, aad: Optional[str] = None) -> Any:
    if not is_encrypted_value(value):
        return copy.deepcopy(value)
    plaintext = decrypt_text(value, aad=aad)
    if plaintext in (None, ""):
        return plaintext
    try:
        return json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise FieldCryptoError("encrypted JSON field did not contain valid JSON") from exc


def _is_sensitive_key(key_path: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key_path))


def encrypt_json_secret_leaves(value: Any, *, aad_prefix: str = "json") -> Any:
    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{path}[{idx}]") for idx, v in enumerate(node)]
        if isinstance(node, str) and node and _is_sensitive_key(path):
            return encrypt_text(node, aad=f"{aad_prefix}:{path}")
        return node

    return walk(copy.deepcopy(value), "")


def decrypt_json_secret_leaves(value: Any, *, aad_prefix: str = "json") -> Any:
    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{path}[{idx}]") for idx, v in enumerate(node)]
        if isinstance(node, str) and is_encrypted_value(node):
            return decrypt_text(node, aad=f"{aad_prefix}:{path}")
        return node

    return walk(copy.deepcopy(value), "")


def redact_secret_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        if value in (None, ""):
            return value
        return "[REDACTED]"
    if isinstance(value, dict):
        return json.loads(json.dumps(value, default=str))
    return value
