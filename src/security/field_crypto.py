"""Application-layer field encryption for DB-backed sensitive data.

The database stores versioned ciphertext. AoiTalk loads the data key from an
OS-protected provider and decrypts only inside the application process.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTION_PREFIX = "enc:v1:"
_ALG = "aes256gcm"
_KEY_ID = "local"
_NONCE_LEN = 12
_MAX_LOCAL_KEY_FILE_BYTES = 4096
_MAX_KEY_PROVIDER_OUTPUT_BYTES = 8192
_KEY_PROVIDER_READ_TIMEOUT_SECONDS = 10.0
_ASCII_WHITESPACE = b"\t\n\v\f\r "
_LOCAL_KEY_LINUX_ONLY_ERROR = (
    "local field crypto key-file fallback is supported only on Linux"
)
_KEY_FILE_MODE_ERROR = (
    "local field crypto key file permissions must be 0400 or 0600; "
    "rotate the key file before retrying"
)
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
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value) or len(value) % 4 == 1:
        raise FieldCryptoError("unsupported encrypted field format")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise FieldCryptoError("unsupported encrypted field format") from None
    if _b64url_encode(decoded) != value:
        raise FieldCryptoError("unsupported encrypted field format")
    return decoded


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _decode_key_provider_output(raw: bytes, provider: str) -> bytes:
    if len(raw) > _MAX_KEY_PROVIDER_OUTPUT_BYTES:
        raise FieldCryptoError(f"{provider} returned an invalid key")
    lines = raw.strip(_ASCII_WHITESPACE).splitlines()
    encoded = lines[-1].strip(_ASCII_WHITESPACE) if lines else b""
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise FieldCryptoError(f"{provider} returned an invalid key") from None
    if base64.b64encode(key) != encoded:
        raise FieldCryptoError(f"{provider} returned an invalid key")
    return key


def _attach_key_provider_cleanup_failure(
    primary_error: FieldCryptoError,
    operation: str,
) -> None:
    primary_error.add_note(f"key provider cleanup also failed: {operation}")


def _close_key_provider_stdout(
    stdout: Any,
    primary_error: Optional[FieldCryptoError],
    provider: str,
) -> Optional[FieldCryptoError]:
    try:
        stdout.close()
    except Exception:
        if primary_error is None:
            primary_error = FieldCryptoError(f"{provider} failed")
        else:
            _attach_key_provider_cleanup_failure(primary_error, "stdout close")

        # A failed IOBase.close() can be retried later by __del__, outside this
        # sanitized error path. Retry now so a transient failure cannot escape there.
        try:
            stdout.close()
        except Exception:
            pass
    return primary_error


def _run_key_provider(command: Any, *, shell: bool, provider: str) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise FieldCryptoError(f"{provider} failed") from None
    stdout = process.stdout
    primary_error: Optional[FieldCryptoError] = None
    raw = b""
    must_kill = False
    wait_completed = False

    if stdout is None:
        primary_error = FieldCryptoError(f"{provider} failed")
        must_kill = True
    else:
        read_result: list[bytes] = []
        read_error: list[BaseException] = []

        def _read_stdout() -> None:
            try:
                read_result.append(stdout.read(_MAX_KEY_PROVIDER_OUTPUT_BYTES + 1))
            except BaseException as exc:
                read_error.append(exc)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()
        reader.join(_KEY_PROVIDER_READ_TIMEOUT_SECONDS)
        if reader.is_alive():
            primary_error = FieldCryptoError(f"{provider} timed out")
            must_kill = True
        elif read_error:
            primary_error = FieldCryptoError(f"{provider} failed")
            must_kill = True
        else:
            raw = read_result[0] if read_result else b""
            if len(raw) > _MAX_KEY_PROVIDER_OUTPUT_BYTES:
                primary_error = FieldCryptoError(f"{provider} returned an invalid key")
                must_kill = True

    if primary_error is None:
        try:
            returncode = process.wait()
            wait_completed = True
        except Exception:
            primary_error = FieldCryptoError(f"{provider} failed")
            must_kill = True
        else:
            if returncode != 0:
                primary_error = FieldCryptoError(
                    f"{provider} failed with exit status {returncode}"
                )

    if must_kill and primary_error is not None:
        try:
            process.kill()
        except Exception:
            _attach_key_provider_cleanup_failure(primary_error, "kill")
    if not wait_completed:
        try:
            process.wait()
        except Exception:
            if primary_error is None:
                primary_error = FieldCryptoError(f"{provider} failed")
            _attach_key_provider_cleanup_failure(primary_error, "wait")
    if stdout is not None:
        primary_error = _close_key_provider_stdout(stdout, primary_error, provider)

    if primary_error is not None:
        raise primary_error
    return _decode_key_provider_output(raw, provider)


def _run_key_command(command: str) -> bytes:
    return _run_key_provider(command, shell=True, provider="field crypto key command")


def _run_windows_dpapi_helper() -> bytes:
    script = _repo_root() / "scripts" / "field_crypto_key.ps1"
    if not script.exists():
        raise FieldCryptoError(f"field crypto key helper not found: {script}")
    return _run_key_provider(
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
        shell=False,
        provider="field crypto DPAPI helper",
    )


def _decode_local_key_file(raw: bytes) -> bytes:
    if len(raw) > _MAX_LOCAL_KEY_FILE_BYTES or any(byte > 0x7F for byte in raw):
        raise FieldCryptoError(
            "local field crypto key file must contain one valid 32-byte base64 key"
        )
    encoded = raw.strip(_ASCII_WHITESPACE)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise FieldCryptoError(
            "local field crypto key file must contain one valid 32-byte base64 key"
        ) from None
    if len(key) != 32 or base64.b64encode(key) != encoded:
        raise FieldCryptoError(
            "local field crypto key file must contain one valid 32-byte base64 key"
        )
    return key


@dataclass(frozen=True)
class _PosixParentContext:
    fd: int
    path: Path
    dev: int
    ino: int
    requires_safe_key: bool


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _trusted_directory_stat(
    directory_stat: os.stat_result,
    *,
    requires_non_writable: bool = False,
) -> bool:
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise FieldCryptoError(
            "local field crypto key parent path must contain only directories"
        )
    if directory_stat.st_uid not in {0, os.geteuid()}:
        raise FieldCryptoError(
            "local field crypto key ancestors must be owned by root or the effective user"
        )
    mode = stat.S_IMODE(directory_stat.st_mode)
    writable = bool(mode & 0o022)
    sticky_world_writable = bool(mode & stat.S_ISVTX and mode & 0o002)
    if writable and (requires_non_writable or not sticky_world_writable):
        if requires_non_writable:
            raise FieldCryptoError(
                "sticky world-writable key ancestor requires a trusted non-writable next component"
            )
        raise FieldCryptoError(
            "local field crypto key ancestors must not be group/world writable"
        )
    return sticky_world_writable


def _close_with_primary(fd: int, primary_error: BaseException, note: str) -> None:
    try:
        os.close(fd)
    except OSError:
        primary_error.add_note(note)


def _walk_local_key_parent(parent_path: Path, *, create_missing: bool) -> _PosixParentContext:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        not sys.platform.startswith("linux")
        or os.name == "nt"
        or no_follow is None
        or directory is None
        or not hasattr(os, "geteuid")
    ):
        raise FieldCryptoError(_LOCAL_KEY_LINUX_ONLY_ERROR)

    flags = os.O_RDONLY | no_follow | directory
    parent_fd = os.open(parent_path.anchor, flags)
    try:
        root_path_stat = os.stat(parent_path.anchor, follow_symlinks=False)
        root_fd_stat = os.fstat(parent_fd)
        if not _same_identity(root_path_stat, root_fd_stat):
            raise FieldCryptoError(
                "local field crypto key ancestor changed during path walk"
            )
        requires_safe_child = _trusted_directory_stat(root_fd_stat)

        for component in parent_path.parts[1:]:
            try:
                component_stat = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                component_stat = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            if stat.S_ISLNK(component_stat.st_mode):
                raise FieldCryptoError(
                    "local field crypto key parent path must not contain symlinks"
                )
            next_fd: Optional[int] = None
            try:
                next_fd = os.open(component, flags, dir_fd=parent_fd)
                opened_stat = os.fstat(next_fd)
                if not _same_identity(component_stat, opened_stat):
                    raise FieldCryptoError(
                        "local field crypto key ancestor changed during path walk"
                    )
                next_requires_safe_child = _trusted_directory_stat(
                    opened_stat,
                    requires_non_writable=requires_safe_child,
                )
            except BaseException as exc:
                if next_fd is not None:
                    _close_with_primary(
                        next_fd,
                        exc,
                        "new key ancestor descriptor cleanup also failed",
                    )
                raise

            old_parent_fd = parent_fd
            parent_fd = next_fd
            next_fd = None
            os.close(old_parent_fd)
            requires_safe_child = next_requires_safe_child

        parent_stat = os.fstat(parent_fd)
        return _PosixParentContext(
            fd=parent_fd,
            path=parent_path,
            dev=parent_stat.st_dev,
            ino=parent_stat.st_ino,
            requires_safe_key=requires_safe_child,
        )
    except BaseException as exc:
        _close_with_primary(
            parent_fd,
            exc,
            "key ancestor descriptor cleanup also failed",
        )
        raise


def _open_local_key_parent(key_path: Path) -> tuple[Path, _PosixParentContext]:
    if not sys.platform.startswith("linux") or os.name == "nt":
        raise FieldCryptoError(_LOCAL_KEY_LINUX_ONLY_ERROR)
    absolute_key_path = Path(os.path.abspath(os.fspath(key_path)))
    return absolute_key_path, _walk_local_key_parent(
        absolute_key_path.parent,
        create_missing=True,
    )


def _validate_posix_key_stat(fd: int, file_stat: os.stat_result) -> os.stat_result:
    if not stat.S_ISREG(file_stat.st_mode):
        raise FieldCryptoError("local field crypto key path must be a regular file")
    if file_stat.st_uid != os.geteuid():
        raise FieldCryptoError(
            "local field crypto key file must be owned by the effective user"
        )

    mode = stat.S_IMODE(file_stat.st_mode)
    if mode in {0o400, 0o600}:
        return file_stat
    raise FieldCryptoError(_KEY_FILE_MODE_ERROR)


def _assert_configured_key_identity(
    key_path: Path,
    parent: _PosixParentContext,
    expected_stat: os.stat_result,
) -> None:
    try:
        current = _walk_local_key_parent(parent.path, create_missing=False)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise FieldCryptoError(
            "local field crypto configured path changed during operation"
        ) from exc
    primary_error: Optional[BaseException] = None
    try:
        if current.dev != parent.dev or current.ino != parent.ino:
            raise FieldCryptoError(
                "local field crypto configured path changed during operation"
            )
        try:
            configured_stat = os.stat(
                key_path.name,
                dir_fd=current.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise FieldCryptoError(
                "local field crypto configured path changed during operation"
            ) from exc
        _validate_posix_key_stat(-1, configured_stat)
        if not _same_identity(configured_stat, expected_stat):
            raise FieldCryptoError(
                "local field crypto configured path changed during operation"
            )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(current.fd)
        except OSError:
            if primary_error is None:
                raise
            primary_error.add_note(
                "configured key parent descriptor cleanup also failed"
            )


def _read_local_key_file_with_identity(
    key_path: Path,
    parent: _PosixParentContext,
) -> tuple[bytes, os.stat_result]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_block = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_block is None:
        raise FieldCryptoError(
            "safe local field crypto key file reads are unavailable on this platform"
        )
    flags = os.O_RDONLY | no_follow | non_block
    fd = os.open(key_path.name, flags, dir_fd=parent.fd)
    primary_error: Optional[BaseException] = None
    try:
        file_stat = os.fstat(fd)
        _validate_posix_key_stat(fd, file_stat)
        raw = bytearray()
        while len(raw) <= _MAX_LOCAL_KEY_FILE_BYTES:
            chunk = os.read(fd, _MAX_LOCAL_KEY_FILE_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        key = _decode_local_key_file(bytes(raw))
        _assert_configured_key_identity(key_path, parent, file_stat)
        return key, file_stat
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            if primary_error is None:
                raise
            primary_error.add_note("key file descriptor cleanup also failed")


def _read_local_key_file(key_path: Path, parent: _PosixParentContext) -> bytes:
    return _read_local_key_file_with_identity(key_path, parent)[0]


def _fsync_parent_directory(parent: _PosixParentContext) -> None:
    os.fsync(parent.fd)


def _unlink_local_temporary(
    temporary_name: str,
    parent: _PosixParentContext,
) -> None:
    os.unlink(temporary_name, dir_fd=parent.fd)


def _cleanup_owned_published_key(
    key_path: Path,
    parent: _PosixParentContext,
    expected_stat: os.stat_result,
    primary_error: BaseException,
) -> None:
    try:
        configured_stat = os.stat(
            key_path.name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if _same_identity(configured_stat, expected_stat):
            os.unlink(key_path.name, dir_fd=parent.fd)
            os.fsync(parent.fd)
    except FileNotFoundError:
        return
    except OSError:
        primary_error.add_note("owned published key cleanup also failed")


def _initialize_local_key_file(key_path: Path, parent: _PosixParentContext) -> bytes:
    key = os.urandom(32)
    encoded_key = base64.b64encode(key)
    temporary_name = (
        f".{key_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    temporary_created = False
    result: Optional[bytes] = None
    expected_stat: Optional[os.stat_result] = None
    published_own_key = False
    primary_error: Optional[BaseException] = None

    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary_name, flags, 0o600, dir_fd=parent.fd)
        temporary_created = True
        fd_primary_error: Optional[BaseException] = None
        try:
            os.fchmod(fd, 0o600)
            written = 0
            while written < len(encoded_key):
                chunk_size = os.write(fd, encoded_key[written:])
                if chunk_size == 0:
                    raise OSError("local field crypto temporary key write made no progress")
                written += chunk_size
            os.fsync(fd)
            expected_stat = os.fstat(fd)
            if stat.S_IMODE(expected_stat.st_mode) != 0o600:
                raise FieldCryptoError(
                    "local field crypto temporary key file must use mode 0600"
                )
        except BaseException as exc:
            fd_primary_error = exc
            raise
        finally:
            try:
                os.close(fd)
            except OSError:
                if fd_primary_error is None:
                    raise
                fd_primary_error.add_note("temporary key file descriptor cleanup also failed")

        try:
            os.link(
                temporary_name,
                key_path.name,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
            result = key
            published_own_key = True
        except FileExistsError:
            result, expected_stat = _read_local_key_file_with_identity(
                key_path,
                parent,
            )
        except OSError as exc:
            raise FieldCryptoError(
                "local field crypto key publication requires hard-link support"
            ) from exc
        if expected_stat is None:
            raise FieldCryptoError("local field crypto key initialization failed")
        _assert_configured_key_identity(key_path, parent, expected_stat)
    except BaseException as exc:
        primary_error = exc

    cleanup_errors: list[tuple[str, OSError]] = []
    if temporary_created:
        try:
            _unlink_local_temporary(temporary_name, parent)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_errors.append(("temporary key file unlink", exc))
        try:
            _fsync_parent_directory(parent)
        except OSError as exc:
            cleanup_errors.append(("key parent directory fsync", exc))

    if primary_error is None and not cleanup_errors and expected_stat is not None:
        try:
            _assert_configured_key_identity(key_path, parent, expected_stat)
        except BaseException as exc:
            primary_error = exc

    if primary_error is not None:
        if published_own_key and expected_stat is not None:
            _cleanup_owned_published_key(
                key_path,
                parent,
                expected_stat,
                primary_error,
            )
        for operation, _ in cleanup_errors:
            primary_error.add_note(f"{operation} also failed")
        raise primary_error
    if cleanup_errors:
        operation, cleanup_error = cleanup_errors[0]
        message = (
            "local field crypto temporary key file cleanup failed"
            if operation == "temporary key file unlink"
            else f"local field crypto {operation} failed"
        )
        error = FieldCryptoError(message)
        for additional_operation, _ in cleanup_errors[1:]:
            error.add_note(f"{additional_operation} also failed")
        raise error from cleanup_error
    if result is None:
        raise FieldCryptoError("local field crypto key initialization failed")
    return result


def _local_key_file() -> bytes:
    if not sys.platform.startswith("linux") or os.name == "nt":
        raise FieldCryptoError(_LOCAL_KEY_LINUX_ONLY_ERROR)
    if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise FieldCryptoError(
            "No OS key provider is configured. On Linux set "
            "AOITALK_FIELD_CRYPTO_KEY_COMMAND to a keyring/KMS command, or "
            "explicitly allow the local key-file fallback."
        )
    configured_key_path = Path(
        os.getenv(
            "AOITALK_FIELD_CRYPTO_LOCAL_KEY_FILE",
            str(Path.home() / ".config" / "aoitalk" / "field-crypto.key"),
        )
    )
    key_path, parent = _open_local_key_parent(configured_key_path)
    primary_error: Optional[BaseException] = None
    try:
        try:
            return _read_local_key_file(key_path, parent)
        except FileNotFoundError:
            return _initialize_local_key_file(key_path, parent)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(parent.fd)
        except OSError:
            if primary_error is None:
                raise
            primary_error.add_note("key parent directory descriptor cleanup also failed")


@lru_cache(maxsize=1)
def get_data_key() -> bytes:
    """Return the 256-bit local data key.

    Environment-provided key material is intentionally disabled by default.
    It is only accepted when AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY=true for tests.
    """

    key_command = os.getenv("AOITALK_FIELD_CRYPTO_KEY_COMMAND")
    env_key = os.getenv("AOITALK_FIELD_CRYPTO_KEY_B64")
    if key_command:
        # Docker may expose the optional compatibility B64 secret as a
        # mounted file while production is configured with a KMS/keyring
        # command.  The stronger provider must win; a disabled test-only env
        # key must not mask the configured production provider.
        key = _run_key_command(key_command)
    elif env_key:
        if os.getenv("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "").lower() not in {
            "1",
            "true",
            "yes",
        }:
            raise FieldCryptoError(
                "AOITALK_FIELD_CRYPTO_KEY_B64 is set but env keys are disabled"
            )
        key = base64.b64decode(env_key)
    elif sys.platform.startswith("win"):
        key = _run_windows_dpapi_helper()
    elif sys.platform.startswith("linux"):
        key = _local_key_file()
    else:
        raise FieldCryptoError(
            "No field crypto key provider is configured; "
            "AOITALK_FIELD_CRYPTO_KEY_COMMAND is required on this platform"
        )

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
    if (
        len(parts) != 6
        or parts[0] != "enc"
        or parts[1] != "v1"
        or parts[2] != _ALG
        or parts[3] != _KEY_ID
    ):
        raise FieldCryptoError("unsupported encrypted field format")
    nonce = _b64url_decode(parts[4])
    ciphertext = _b64url_decode(parts[5])
    if len(nonce) != _NONCE_LEN or len(ciphertext) < 16:
        raise FieldCryptoError("unsupported encrypted field format")
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
