"""One-task review of host instructions under the adapter's trusted-host premise.

Snapshots detect observed changes; the official RPC does not attest consumed
bytes. No approval is inferred from inspecting a file or from an admin role.
"""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from .codex_task_completion import (
    CodexTaskProtocolError, SUPPORTED_CODEX_VERSIONS, _default_codex_home,
    codex_app_server_command, isolated_process_environment,
)

MAX_SOURCE_BYTES = 256 * 1024
REVIEW_TTL_SEC = 300


def _identity(path: Path, *, directory: bool = False) -> list[int]:
    info = path.lstat()
    if (path.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or path.resolve(strict=True) != path
            or (not directory and info.st_nlink != 1)):
        raise CodexTaskProtocolError("codex_instruction_review_source_unverified")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] if not directory else [info.st_dev, info.st_ino]


def host_snapshot() -> dict[str, Any]:
    """Read only the two official global candidates; never auth/config contents."""
    try:
        environment = isolated_process_environment()
        home = Path(environment["CODEX_HOME"]) if "CODEX_HOME" in environment else _default_codex_home(environment)
        home_identity = _identity(home, directory=True)
        sources = []
        selected = False
        for name in ("AGENTS.override.md", "AGENTS.md"):
            path = home / name
            try:
                before = _identity(path)
            except FileNotFoundError:
                sources.append({"path": str(path), "exists": False, "sha256": None,
                                "size_bytes": 0, "identity": None, "selected": False, "text": None})
                continue
            if before[2] > MAX_SOURCE_BYTES:
                raise CodexTaskProtocolError("codex_instruction_review_source_too_large")
            with path.open("rb") as source:
                raw = source.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES or _identity(path) != before:
                raise CodexTaskProtocolError("codex_instruction_review_source_changed")
            text = raw.decode("utf-8")
            active = bool(text.strip()) and not selected
            selected = selected or active
            sources.append({"path": str(path), "exists": True,
                            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
                            "identity": before, "selected": active, "text": text})
        if _identity(home, directory=True) != home_identity:
            raise CodexTaskProtocolError("codex_instruction_review_source_changed")
        return {"home": str(home), "home_identity": home_identity, "sources": sources}
    except CodexTaskProtocolError:
        raise
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise CodexTaskProtocolError("codex_instruction_review_source_unverified") from exc


def snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {**snapshot, "sources": [{k: v for k, v in s.items() if k != "text"} for s in snapshot["sources"]]}


def cli_snapshot() -> dict[str, Any]:
    """Bounded native --version only; no daemon, account read or model turn."""
    try:
        command = codex_app_server_command()
        path = Path(command[0]).resolve(strict=True)
        before = _identity(path)
        result = subprocess.run([str(path), "--version"], capture_output=True, timeout=10,
                                env=isolated_process_environment(), check=True)
        version = result.stdout.decode("utf-8").strip().removeprefix("codex-cli ")
        if version not in SUPPORTED_CODEX_VERSIONS or _identity(path) != before:
            raise CodexTaskProtocolError("codex_instruction_review_cli_unverified")
    except CodexTaskProtocolError:
        raise
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as exc:
        raise CodexTaskProtocolError("codex_instruction_review_cli_unverified") from exc
    return {"path": str(path), "identity": before, "version": version}


def review_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ReviewedHostInstructions:
    """Private server-issued receipt, checked before model and guarded tool use."""

    def __init__(self, receipt: dict[str, Any], *, task_id: str, owner: str,
                 fingerprint: str, boot_id: str):
        self.receipt = receipt
        self.binding = {"task_id": task_id, "owner": owner, "task_fingerprint": fingerprint, "boot_id": boot_id}

    def check(self) -> None:
        if (any(self.receipt.get(k) != v for k, v in self.binding.items())
                or time.time() >= self.receipt["execution_expires_at"]):
            raise CodexTaskProtocolError("codex_instruction_review_expired")
        if snapshot_metadata(host_snapshot()) != self.receipt["host"]:
            raise CodexTaskProtocolError("codex_instruction_review_source_changed")
        try:
            cli = self.receipt["cli"]
            if _identity(Path(cli["path"])) != cli["identity"]:
                raise CodexTaskProtocolError("codex_instruction_review_cli_changed")
        except (OSError, RuntimeError) as exc:
            raise CodexTaskProtocolError("codex_instruction_review_cli_changed") from exc

    def verify_command(self, command: list[str]) -> None:
        self.check()
        try:
            path = Path(command[0]).resolve(strict=True)
        except (OSError, RuntimeError, IndexError) as exc:
            raise CodexTaskProtocolError("codex_instruction_review_cli_changed") from exc
        if str(path) != self.receipt["cli"]["path"]:
            raise CodexTaskProtocolError("codex_instruction_review_cli_changed")

    def verify_cli(self, command: list[str], user_agent: str) -> None:
        self.verify_command(command)
        if ("/" + self.receipt["cli"]["version"] + " ") not in user_agent + " ":
            raise CodexTaskProtocolError("codex_instruction_review_cli_changed")

    def verify_sources(self, sources: Any) -> None:
        self.check()
        expected = [s["path"] for s in self.receipt["host"]["sources"] if s["selected"]]
        if sources != expected:
            raise CodexTaskProtocolError("codex_instruction_review_source_changed")
