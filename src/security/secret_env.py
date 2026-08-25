"""Cross the Enterprise secret-file boundary without leaking secret values.

``deploy/enterprise/secret-schema.json`` is the canonical contract shared by
the launcher, Compose bundle, and tests.  The Python helper remains useful for
non-container launchers, but the Enterprise entrypoint performs the read while
running as root and removes every ``*_FILE`` variable before dropping uid.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any, Mapping, MutableMapping


_MAX_SECRET_FILE_BYTES = 8 * 1024
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "deploy" / "enterprise" / "secret-schema.json"

# Kept only for installed/non-source launchers where deployment assets are not
# packaged.  The repository and Enterprise image always use the JSON contract.
_FALLBACK_SECRET_ENV_NAMES: tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "NEXTAUTH_SECRET",
    "AOITALK_WEB_AUTH_SECRET",
    "AOITALK_JWT_SECRET",
    "AOITALK_APP_BRIDGE_SECRET",
    "AUTH_SECRET",
    "INTERNAL_API_KEY",
    "AOITALK_CADDY_GATE_KEY",
    "AOITALK_BOOTSTRAP_ADMIN_PASSWORD",
    "AOITALK_FIELD_CRYPTO_KEY_B64",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "MOONSHOT_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "NIJIVOICE_API_KEY",
    "OPENWEATHER_API_KEY",
    "WEBEX_CLIENT_SECRET",
    "WEBEX_STATE_SECRET",
    "DEEPSEEK_API_KEY",
    "DEEPINFRA_TOKEN",
    "DISCORD_BOT_TOKEN",
    "OLLAMA_API_KEY",
    "OPENAI_COMPATIBLE_LOCAL_API_KEY",
    "SGLANG_API_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_API_KEY",
    "OPENAI_FREE_TEAM_API_KEY",
    "GEMINI_FREE_API_KEY",
    "OPENROUTER_FREE_TEAM_API_KEY",
    "GEMINI_PROMO_API_KEY",
)


def _fallback_schema() -> dict[str, Any]:
    return {
        "schema_version": 0,
        "max_bytes": _MAX_SECRET_FILE_BYTES,
        "secrets": [
            {
                "env": name,
                "file": name.lower(),
                "required": False,
                "kind": "legacy",
                "services": ["aoitalk"],
            }
            for name in _FALLBACK_SECRET_ENV_NAMES
        ],
        "providers": {},
    }


def _load_schema() -> dict[str, Any]:
    try:
        raw = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _fallback_schema()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Enterprise secret schema cannot be loaded") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Enterprise secret schema is invalid")
    entries = raw.get("secrets")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Enterprise secret schema has no secrets")
    try:
        max_bytes = int(raw.get("max_bytes", _MAX_SECRET_FILE_BYTES))
    except (TypeError, ValueError):
        raise RuntimeError("Enterprise secret schema has an invalid size limit") from None
    if max_bytes < 1 or max_bytes > _MAX_SECRET_FILE_BYTES:
        raise RuntimeError("Enterprise secret schema has an unsafe size limit")
    seen_env: set[str] = set()
    seen_file: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("Enterprise secret schema contains an invalid entry")
        env_name = item.get("env")
        file_name = item.get("file")
        if (
            not isinstance(env_name, str)
            or not _ENV_NAME_RE.fullmatch(env_name)
            or env_name in seen_env
            or not isinstance(file_name, str)
            or not _FILE_NAME_RE.fullmatch(file_name)
            or file_name in seen_file
        ):
            raise RuntimeError("Enterprise secret schema contains an invalid name")
        if not isinstance(item.get("required", False), bool):
            raise RuntimeError("Enterprise secret schema contains an invalid requirement")
        seen_env.add(env_name)
        seen_file.add(file_name)
    raw["max_bytes"] = max_bytes
    return raw


SECRET_SCHEMA: dict[str, Any] = _load_schema()
SECRET_SCHEMA_PATH = _SCHEMA_PATH
SECRET_SPECS: tuple[dict[str, Any], ...] = tuple(SECRET_SCHEMA["secrets"])
SECRET_ENV_NAMES: tuple[str, ...] = tuple(spec["env"] for spec in SECRET_SPECS)
SECRET_FILE_NAMES: tuple[str, ...] = tuple(spec["file"] for spec in SECRET_SPECS)
PROVIDER_SECRET_NAMES: Mapping[str, tuple[str, ...]] = {
    str(provider): tuple(
        value
        for value in (details.get("env"), *(details.get("aliases", []) or []))
        if isinstance(value, str)
    )
    for provider, details in (SECRET_SCHEMA.get("providers") or {}).items()
    if isinstance(provider, str) and isinstance(details, dict)
}


def _strict_permissions(target: Mapping[str, str]) -> bool:
    """Enable host secret ownership checks for Enterprise/direct root launchers."""

    return (
        str(target.get("AOITALK_PROFILE", "")).strip().lower() == "enterprise"
        or str(target.get("AOITALK_REQUIRE_SECRET_FILE_PERMISSIONS", "")).strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _read_secret(path_text: str, name: str, *, strict_permissions: bool) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        raise RuntimeError(f"Unable to read secret file for {name}")
    try:
        metadata = path.lstat()
    except (OSError, ValueError):
        raise RuntimeError(f"Unable to read secret file for {name}") from None
    # lstat + S_ISREG deliberately rejects symlinked and device/FIFO paths.
    if not stat_module.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RuntimeError(f"Unable to read secret file for {name}")
    if strict_permissions and (
        metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat_module.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError(f"Unable to read secret file for {name}")
    try:
        if metadata.st_size > _MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"Secret file for {name} exceeds the size limit")
        raw = path.read_bytes()
    except RuntimeError:
        raise
    except (OSError, UnicodeError):
        raise RuntimeError(f"Unable to read secret file for {name}") from None
    if len(raw) > _MAX_SECRET_FILE_BYTES:
        raise RuntimeError(f"Secret file for {name} exceeds the size limit")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(f"Secret file for {name} contains an invalid value") from None
    value = value.rstrip("\r\n")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise RuntimeError(f"Secret file for {name} contains an invalid value")
    return value


def load_secret_environment(
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Materialize canonical ``*_FILE`` values and remove the file settings.

    File values take precedence over an existing plaintext value.  Secret
    values and reader exceptions are never included in raised messages.
    """

    target = os.environ if environ is None else environ
    strict_permissions = _strict_permissions(target)
    for spec in SECRET_SPECS:
        name = spec["env"]
        file_name = f"{name}_FILE"
        file_path = str(target.get(file_name, "")).strip()
        if not file_path:
            continue
        value = _read_secret(file_path, name, strict_permissions=strict_permissions)
        target[name] = value
        # Do not let a child process retry or discover the root-only path.
        target.pop(file_name, None)


__all__ = [
    "PROVIDER_SECRET_NAMES",
    "SECRET_ENV_NAMES",
    "SECRET_FILE_NAMES",
    "SECRET_SCHEMA",
    "SECRET_SCHEMA_PATH",
    "SECRET_SPECS",
    "load_secret_environment",
]
