"""Read-only, bounded access to the Grok Bot desktop profile."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Protocol

from ..codes import AgentLinkFailure, LinkErrorCode


PROFILE_FILES = frozenset({"sand-secrets.json", "Local State", "desktop-status.json"})
MAX_PROFILE_BYTES = 2 * 1024 * 1024


class ProfileReader(Protocol):
    def read(self, name: str) -> str: ...


class WindowsProfileReader:
    """Only open explicitly permitted files; never write or refresh credentials."""

    def __init__(
        self,
        app_data: str | Path | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        if (sys.platform if platform is None else platform) != "win32":
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
        selected = os.environ.get("APPDATA") if app_data is None else app_data
        if not isinstance(selected, (str, Path)) or not str(selected):
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
        self._root = Path(selected) / "Grok Bot"

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or name not in PROFILE_FILES:
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
        return self._root / name

    def read(self, name: str) -> str:
        path = self._path(name)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_PROFILE_BYTES + 1)
            if len(raw) > MAX_PROFILE_BYTES:
                raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
            return raw.decode("utf-8")
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE) from None

    def exists(self, name: str) -> bool:
        path = self._path(name)
        try:
            return path.is_file()
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE) from None
