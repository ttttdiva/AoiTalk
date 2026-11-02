"""One explicitly approved, server-owned stdio/Tunnel DOTS connection.

This identity is a deployment binding, not OAuth and not a general MCP caller
identity.  The capability is a separate bearer credential used only between
the private Plugin process and the local Aoi API bridge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from dotenv import dotenv_values

from .paths import PACKAGE_ROOT


EXPECTED_APP_ID = "asdk_app_6adb40e292bc8191b9b56326c651cd65"
EXPECTED_PLUGIN_ID = "plugin_asdk_app_6adb40e292bc8191b9b56326c651cd65"
EXPECTED_TUNNEL_ID = "tunnel_6adb0ad667388191a4d38f365d609fdb"
EXPECTED_RUNTIME_ALIAS = "aoitalk-41"
EXPECTED_PROFILE_ALIAS = "aoitalk-plugin-41"
EXPECTED_OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f"

ENABLED_KEY = "AOITALK_DOTS_DIALOGUE_DEDICATED_ENABLED"
CONVERSATION_KEY = "AOITALK_DOTS_DIALOGUE_DEDICATED_CONVERSATION_ID"
CAPABILITY_KEY = "AOITALK_DOTS_DIALOGUE_DEDICATED_CAPABILITY"
TUNNEL_KEY = "HOME_MCP_AOITALK_TUNNEL_ID"
API_URL_KEY = "HOME_MCP_AOITALK_URL"
_CONFIG_KEYS = frozenset({ENABLED_KEY, CONVERSATION_KEY, CAPABILITY_KEY, TUNNEL_KEY, API_URL_KEY})


class DedicatedDotsConfigurationError(ValueError):
    """Safe configuration error; never includes values or secret material."""

    def __init__(self, code: str = "dedicated_connection_unavailable"):
        self.code = code
        super().__init__(code)


def _canonical_identity(values: tuple[str, ...]) -> bytes:
    return json.dumps(["aoitalk.dots.dedicated.v1", *values], separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


@dataclass(frozen=True, slots=True)
class DedicatedDotsPrincipal:
    """Server-configured identity presented to the existing dialogue ledger."""

    app_id: str
    plugin_id: str
    tunnel_id: str
    runtime_alias: str
    profile_alias: str
    owner_user_id: str
    conversation_id: str

    @property
    def fingerprint(self) -> str:
        try:
            owner_id = str(UUID(self.owner_user_id))
            UUID(self.conversation_id)
        except (TypeError, ValueError, AttributeError):
            raise DedicatedDotsConfigurationError() from None
        identity = (self.app_id, self.plugin_id, self.tunnel_id, self.runtime_alias,
                    self.profile_alias, owner_id)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in identity):
            raise DedicatedDotsConfigurationError()
        return hashlib.sha256(_canonical_identity(identity)).hexdigest()


@dataclass(frozen=True, slots=True)
class DedicatedDotsConnectionConfig:
    """Validated opt-in configuration for the one private Plugin/Tunnel."""

    conversation_id: str
    capability: str = field(repr=False)
    app_id: str = EXPECTED_APP_ID
    plugin_id: str = EXPECTED_PLUGIN_ID
    tunnel_id: str = EXPECTED_TUNNEL_ID
    runtime_alias: str = EXPECTED_RUNTIME_ALIAS
    profile_alias: str = EXPECTED_PROFILE_ALIAS
    owner_user_id: str = EXPECTED_OWNER_ID
    api_base_url: str = "http://127.0.0.1:3000"
    enabled: bool = True

    def __post_init__(self) -> None:
        identities = (self.app_id, self.plugin_id, self.tunnel_id, self.runtime_alias,
                      self.profile_alias, self.owner_user_id)
        expected = (EXPECTED_APP_ID, EXPECTED_PLUGIN_ID, EXPECTED_TUNNEL_ID,
                    EXPECTED_RUNTIME_ALIAS, EXPECTED_PROFILE_ALIAS, EXPECTED_OWNER_ID)
        if self.enabled is not True or identities != expected:
            raise DedicatedDotsConfigurationError()
        try:
            normalized_conversation = str(UUID(self.conversation_id))
        except (TypeError, ValueError, AttributeError):
            raise DedicatedDotsConfigurationError() from None
        object.__setattr__(self, "conversation_id", normalized_conversation)
        if (not isinstance(self.capability, str) or len(self.capability) < 48
                or len(self.capability) > 512 or any(ch.isspace() for ch in self.capability)):
            raise DedicatedDotsConfigurationError()
        if (not isinstance(self.api_base_url, str) or not self.api_base_url.strip()
                or len(self.api_base_url) > 512 or any(ch.isspace() for ch in self.api_base_url)):
            raise DedicatedDotsConfigurationError()
        try:
            parsed = urlsplit(self.api_base_url)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
            if parsed.scheme.lower() == "http":
                try:
                    loopback = parsed.hostname.lower() == "localhost" or ipaddress.ip_address(
                        parsed.hostname
                    ).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    raise ValueError
        except (UnicodeError, TypeError, ValueError):
            raise DedicatedDotsConfigurationError() from None

    @property
    def principal(self) -> DedicatedDotsPrincipal:
        return DedicatedDotsPrincipal(
            app_id=self.app_id,
            plugin_id=self.plugin_id,
            tunnel_id=self.tunnel_id,
            runtime_alias=self.runtime_alias,
            profile_alias=self.profile_alias,
            owner_user_id=self.owner_user_id,
            conversation_id=self.conversation_id,
        )

    def accepts_capability(self, candidate: Any) -> bool:
        if not isinstance(candidate, str) or len(candidate) > 512:
            return False
        expected = hashlib.sha256(self.capability.encode("utf-8")).digest()
        presented = hashlib.sha256(candidate.encode("utf-8")).digest()
        return hmac.compare_digest(presented, expected)


def load_dedicated_dots_connection_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> DedicatedDotsConnectionConfig | None:
    """Read only the four connection keys; disabled or incomplete is fail-closed."""

    env = os.environ if environ is None else environ
    if config_path is None:
        configured_path = env.get("AOITALK_PLUGIN_CONFIG_FILE", "").strip()
        path = Path(configured_path).expanduser() if configured_path else PACKAGE_ROOT / ".env"
        if not path.is_absolute():
            path = PACKAGE_ROOT / path
    else:
        path = Path(config_path).expanduser()
    try:
        file_values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.is_file() else {}
    except (OSError, UnicodeError):
        raise DedicatedDotsConfigurationError() from None
    values = {
        key: env.get(key, file_values.get(key))
        for key in _CONFIG_KEYS
    }
    if values.get(ENABLED_KEY) != "true":
        return None
    if values.get(TUNNEL_KEY) != EXPECTED_TUNNEL_ID:
        raise DedicatedDotsConfigurationError()
    try:
        return DedicatedDotsConnectionConfig(
            conversation_id=values.get(CONVERSATION_KEY),
            capability=values.get(CAPABILITY_KEY),
            api_base_url=values.get(API_URL_KEY) or "http://127.0.0.1:3000",
        )
    except DedicatedDotsConfigurationError:
        raise


__all__ = [
    "CAPABILITY_KEY",
    "API_URL_KEY",
    "CONVERSATION_KEY",
    "ENABLED_KEY",
    "DedicatedDotsConfigurationError",
    "DedicatedDotsConnectionConfig",
    "DedicatedDotsPrincipal",
    "EXPECTED_APP_ID",
    "EXPECTED_OWNER_ID",
    "EXPECTED_PLUGIN_ID",
    "EXPECTED_PROFILE_ALIAS",
    "EXPECTED_RUNTIME_ALIAS",
    "EXPECTED_TUNNEL_ID",
    "TUNNEL_KEY",
    "load_dedicated_dots_connection_config",
]
