"""Environment helpers for subprocesses launched by AoiTalk.

The agent/shell subprocesses should use the same Python installation as the
running AoiTalk process without rewriting command strings or changing any
permission boundaries.  This module keeps that contract in one place so the
foreground command executor, background jobs, and CLI backends can all share
it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional


# Environment names needed to start ordinary command-line tools.  This is
# intentionally a small, static allowlist: copying ``os.environ`` here would
# make every shell/worker inherit AoiTalk credentials (database, LLM, auth,
# and browser state) by accident.  A caller that has a legitimate additional
# requirement must pass it explicitly through ``extra_env`` or
# ``inherit_keys``.
SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        # Windows process/runtime requirements.
        "COMSPEC",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
        # Temporary files and locale/terminal behaviour used by common tools.
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "TERM",
        "COLORTERM",
    }
)

# Known AoiTalk/host credentials are never copied through the generic child
# environment helper, even when a caller accidentally passes a whole config
# mapping as ``extra_env``.  A dedicated parent-owned provider path may still
# pass a narrowly scoped credential through its own explicit launcher; local
# worker/MCP/command children must not receive these names by default.
SUBPROCESS_SECRET_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "AOITALK_WEB_AUTH_SECRET",
        "AOITALK_AUTH_SECRET",
        "JWT_SECRET",
        "SECRET_KEY",
        "CHATGPT_SESSION_STATE",
        "BROWSER_LOGIN_STATE",
    }
)


def _env_value(base: Mapping[str, str], name: str) -> Optional[str]:
    """Read an environment name, handling Windows' case-insensitive keys."""

    value = base.get(name)
    if value is not None:
        return str(value)
    if os.name != "nt":
        return None

    normalized = name.upper()
    for key, candidate in base.items():
        if str(key).upper() == normalized:
            return str(candidate)
    return None


def _is_sensitive_env_name(name: str) -> bool:
    normalized = str(name).upper()
    if normalized in SUBPROCESS_SECRET_ENV_NAMES:
        return True
    return any(
        marker in normalized
        for marker in (
            "API_KEY",
            "ACCESS_KEY",
            "PASSWORD",
            "TOKEN",
            "SECRET",
            "AUTH_SECRET",
            "AUTH",
            "SESSION",
            "CREDENTIAL",
            "SESSION_STATE",
            "COOKIE",
            "DATABASE_URL",
            "DB_PASSWORD",
            "PRIVATE_KEY",
            "CLIENT_SECRET",
        )
    )


def _path_key(path: str) -> Optional[str]:
    """Return a comparison key for a PATH entry.

    Empty PATH entries have a special meaning (the current working directory)
    and must be preserved rather than treated as the executable directory.
    ``normcase`` follows the host platform, so Windows comparisons are
    case-insensitive while POSIX comparisons remain case-sensitive.
    """

    if not path:
        return None
    return os.path.normcase(os.path.abspath(path))


def _running_in_virtual_environment() -> bool:
    """Return whether the current interpreter is running inside a venv.

    ``base_prefix`` is the standard venv indicator.  ``real_prefix`` covers
    older ``virtualenv`` versions that predate ``base_prefix``.
    """

    real_prefix = getattr(sys, "real_prefix", None)
    if real_prefix:
        return True
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix


def build_aoitalk_subprocess_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    extra_env: Optional[Mapping[str, str]] = None,
    inherit_keys: Iterable[str] = (),
    sensitive_env_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Build an isolated environment for an AoiTalk subprocess.

    Args:
        base: Optional source environment.  It is *read* but never copied in
            full or mutated; when omitted, the current process environment is
            used as the source for the allowlisted values.
        extra_env: Explicit values to add for this child.  Non-sensitive
            values are added directly; a sensitive value also requires its
            exact name in ``sensitive_env_keys`` (for example, a configured
            MCP token).
        inherit_keys: Additional names to copy from ``base`` explicitly.  It
            is useful for a caller that has verified a runtime-specific value
            is required without making that value part of the global default
            allowlist.
        sensitive_env_keys: Exact names from ``extra_env`` that the trusted
            parent launcher intentionally authorizes (for example, an MCP
            server's configured API token).  Sensitive names are still denied
            when they come from ``base`` or ``inherit_keys``; callers must
            provide the value explicitly.

    Returns:
        A new minimal environment mapping with AoiTalk's runtime Python
        exposed as ``AOITALK_PYTHON`` and its executable directory prepended
        to ``PATH``.  Parent credentials are absent unless explicitly passed.
    """

    source = os.environ if base is None else base
    explicitly_authorized_sensitive = {
        str(key).upper() for key in sensitive_env_keys
    }
    requested_keys = set(SUBPROCESS_ENV_ALLOWLIST)
    requested_keys.update(
        str(key) for key in inherit_keys if not _is_sensitive_env_name(str(key))
    )
    env: dict[str, str] = {}
    for key in requested_keys:
        value = _env_value(source, key)
        if value is not None:
            env[key] = value

    # Explicit child configuration is applied after the parent allowlist.  A
    # caller can therefore intentionally override PATH or provide a scoped
    # provider/MCP credential without exposing all of AoiTalk's environment.
    if extra_env:
        for key, value in extra_env.items():
            normalized_key = str(key).upper()
            if (
                _is_sensitive_env_name(normalized_key)
                and normalized_key not in explicitly_authorized_sensitive
            ):
                continue
            env[str(key)] = str(value)

    # The process that is actually running AoiTalk is the source of truth;
    # do not hard-code a repository or platform-specific venv path.
    python_executable = str(sys.executable)
    env["AOITALK_PYTHON"] = python_executable

    if _running_in_virtual_environment():
        env["VIRTUAL_ENV"] = str(sys.prefix)

    # Keep existing allowlisted PATH entries (including empty entries), while
    # avoiding a duplicate of the runtime executable directory.  Empty PATH is
    # valid and still receives the runtime directory safely.
    python_bin_dir = str(Path(python_executable).parent)
    existing_path = env.get("PATH", "")
    entries = existing_path.split(os.pathsep) if existing_path else []
    python_bin_key = _path_key(python_bin_dir)
    already_present = any(
        entry_key is not None and entry_key == python_bin_key
        for entry_key in (_path_key(entry) for entry in entries)
    )
    if not already_present:
        env["PATH"] = os.pathsep.join([python_bin_dir, *entries])
    elif "PATH" not in env:
        # Defensive fallback for unusual Mapping implementations that report
        # no PATH while yielding an empty value through ``get``.
        env["PATH"] = python_bin_dir

    # Preserve the existing UTF-8 subprocess contract used by command tools.
    env["PYTHONIOENCODING"] = "utf-8"
    # A child can otherwise rehydrate the parent application's secrets by
    # importing python-dotenv (many bundled MCP servers call load_dotenv at
    # import time).  Configuration that a child genuinely needs must be
    # expanded by the parent and passed through ``extra_env`` explicitly.
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return env


__all__ = [
    "SUBPROCESS_ENV_ALLOWLIST",
    "SUBPROCESS_SECRET_ENV_NAMES",
    "build_aoitalk_subprocess_env",
]
