"""Read-only identity of the existing Muse CLI authentication epoch.

This is not a login, a credential issuer, or a vault.  Version 1 hashes a
domain-separated canonical tuple of the dedicated HOME, effective cookie
path, configured VM/node and exact cookie-file bytes.  Any cookie-file byte
change conservatively invalidates the epoch, including harmless formatting.
Authentication validity and assigned-VM proof require a separate provider GET.
Never log the returned fingerprint or the private state represented here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re


_DOMAIN = "aoitalk.muse.existing-auth-epoch.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,256}$")


class MuseExistingAuthError(ValueError):
    """A deliberately value-free failure at the private state boundary."""

    def __init__(self) -> None:
        super().__init__("Muse existing authentication state is unavailable")


@dataclass(frozen=True)
class MuseExistingAuthState:
    state_root: Path = field(repr=False)
    cookie_path: Path = field(repr=False)
    vm_id: str = field(repr=False)
    node_id: str = field(repr=False)
    auth_fingerprint: str = field(repr=False)


def _cookie_header(raw: str) -> str:
    """Mirror the pinned muse-cli 0.3.2 gateway.load_cookies interpretation."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                cookies = value.get("cookies")
                value = cookies if isinstance(cookies, dict) else value
                return "; ".join(f"{key}={item}" for key, item in value.items())
        except ValueError:
            pass
    if "hatch_sess=" in raw or "datr=" in raw:
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line and ";" in line:
                return line
        return " ".join(raw.split())
    parts = []
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) >= 7:
            parts.append(f"{columns[5]}={columns[6]}")
    return "; ".join(parts)


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if not value or len(value) > limit:
        raise MuseExistingAuthError()
    return value


def inspect_existing_muse_auth(state_root: Path | str) -> MuseExistingAuthState:
    """Inspect existing files only; never call CLI load_config or auth helpers.

    Relative cookie paths have the CLI child's state-root cwd semantics.
    Both config and effective cookie files must remain inside that HOME.
    VM/node must already exist: environment overrides and generation are banned.
    """
    try:
        root = Path(state_root)
        if not root.is_absolute():
            raise MuseExistingAuthError()
        root = root.resolve(strict=True)
        if not root.is_dir() or root == Path.home().resolve():
            raise MuseExistingAuthError()
        config_path = (root / ".config" / "muse-cli" / "config.json").resolve(strict=True)
        if not config_path.is_relative_to(root):
            raise MuseExistingAuthError()
        config = json.loads(_read_bounded(config_path, 64 * 1024).decode("utf-8"))
        if not isinstance(config, dict):
            raise MuseExistingAuthError()
        vm, node = config.get("vm_id"), config.get("node_id")
        if not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in (vm, node)):
            raise MuseExistingAuthError()
        cookie_value = config.get("cookies_file", str(root / ".config" / "muse-cli" / "cookies.txt"))
        if not isinstance(cookie_value, str) or not cookie_value.strip():
            raise MuseExistingAuthError()
        cookie_path = Path(cookie_value)
        if not cookie_path.is_absolute():
            cookie_path = root / cookie_path
        cookie_path = cookie_path.resolve(strict=True)
        if not cookie_path.is_relative_to(root):
            raise MuseExistingAuthError()
        cookie_bytes = _read_bounded(cookie_path, 256 * 1024)
        header = _cookie_header(cookie_bytes.decode("utf-8"))
        if not any(part.strip().startswith("hatch_sess=") and part.strip() != "hatch_sess=" for part in header.split(";")):
            raise MuseExistingAuthError()
        material = {
            "contract": _DOMAIN,
            "state_root": os.path.normcase(str(root)),
            "cookie_path": os.path.normcase(str(cookie_path)),
            "vm_id": vm,
            "node_id": node,
            "cookie_bytes_sha256": hashlib.sha256(cookie_bytes).hexdigest(),
        }
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return MuseExistingAuthState(root, cookie_path, vm, node, fingerprint)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise MuseExistingAuthError() from None
