"""Small, dependency-free filename policy shared by execution boundaries."""

from __future__ import annotations

import re
from os import PathLike


# Keep this module free of application imports: it is used while constructing
# the WSL namespace, before the heavier service modules are safe to import.
SENSITIVE_FILE_PATTERNS = (
    re.compile(r"(?i)^\.env(?:\.(?!example$|sample$|template$|dist$)[^/\\]+)?$"),
    re.compile(r"(?i)\.(?:pem|key|p12|pfx|jks|keystore|kdbx|secret|secrets)$"),
    re.compile(r"(?i)^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$"),
    re.compile(r"(?i)^(?:\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|credentials(?:\.json)?)$"),
    re.compile(r"(?i)^secrets?\.(?:json|ya?ml|toml|ini|env)$"),
)

# These names include credential-bearing directories and private key stores.
# The relative-path predicate applies them to every path component; the
# basename predicate below intentionally remains narrow for file APIs that
# need to decide whether one named file may be opened.
SENSITIVE_PATH_COMPONENTS = frozenset(
    {
        "credentials",
        "credential",
        "secrets",
        "secret",
        ".ssh",
    }
)


def _basename(path: str | PathLike[str]) -> str:
    return re.split(r"[\\/]", str(path or "").rstrip("\\/"))[-1]


def is_sensitive_file_name(path: str | PathLike[str]) -> bool:
    """Return whether one basename is credential-like."""

    name = _basename(path)
    return bool(name) and any(pattern.search(name) for pattern in SENSITIVE_FILE_PATTERNS)


def is_sensitive_relative_path(path: str | PathLike[str]) -> bool:
    """Return whether a relative path contains a credential-like component."""

    parts = [part for part in re.split(r"[\\/]", str(path or "")) if part]
    return any(
        part.casefold() in SENSITIVE_PATH_COMPONENTS or is_sensitive_file_name(part)
        for part in parts
    )


# Compatibility spelling used by the existing local-agent file policy.
is_secret_file_name = is_sensitive_file_name


__all__ = [
    "SENSITIVE_FILE_PATTERNS",
    "SENSITIVE_PATH_COMPONENTS",
    "is_sensitive_file_name",
    "is_sensitive_relative_path",
    "is_secret_file_name",
]
