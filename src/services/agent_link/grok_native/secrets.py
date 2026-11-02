"""Decrypt desktop credentials while pinning the selected account and team."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..codes import AgentLinkFailure, LinkErrorCode
from .dpapi import DPAPIUnprotector
from .profile import MAX_PROFILE_BYTES, PROFILE_FILES, ProfileReader


_APP_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?")
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
NO_SELECTED_TEAM = "__no_selected_team__"


@dataclass(frozen=True)
class AccountPin:
    active_identity: str = field(repr=False)
    access_token_ciphertext: str = field(repr=False)
    team_ciphertext: str | None = field(repr=False)


@dataclass(frozen=True)
class GrokCredentials:
    app_version: str = field(repr=False)
    access_token: str = field(repr=False)
    team_id: str | None = field(repr=False)
    pin: AccountPin = field(repr=False)


def _read_json(reader: ProfileReader, name: str) -> dict:
    if name not in PROFILE_FILES:
        raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
    try:
        raw = reader.read(name)
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ValueError
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE) from None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED) from None


def _app_version(reader: ProfileReader) -> str:
    status = _read_json(reader, "desktop-status.json")
    if status.get("signedIn") is not True:
        raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
    version = status.get("appVersion")
    if not isinstance(version, str) or not _APP_VERSION.fullmatch(version):
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    return version


def _account_pin(reader: ProfileReader) -> AccountPin:
    stored = _read_json(reader, "sand-secrets.json")
    try:
        version = stored.get("version")
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError
        accounts_record = json.loads(stored["cursor-accounts"])
        active = accounts_record["active"]
        accounts = accounts_record["accounts"]
        if not isinstance(active, str) or not active or not isinstance(accounts, dict):
            raise ValueError
        selected = accounts[active]
        access = selected["cursor-access-token"]
        team = selected.get("cursor-selected-team-id")
        if not isinstance(access, str) or not access:
            raise ValueError
        if team is not None and (not isinstance(team, str) or not team):
            raise ValueError
        return AccountPin(active, access, team)
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED) from None


def assert_account_pin(profile_reader: ProfileReader, pin: AccountPin) -> None:
    """Fail closed if identity, token, selected team, or sign-in state changed."""
    try:
        _app_version(profile_reader)
        current = _account_pin(profile_reader)
        if not isinstance(pin, AccountPin) or current != pin:
            raise ValueError
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_ACCOUNT_CHANGED) from None


def _decode_base64(value: object, code: LinkErrorCode) -> bytes:
    try:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_PROFILE_BYTES
            or not _BASE64.fullmatch(value)
        ):
            raise ValueError
        decoded = base64.b64decode(value, validate=True)
        if not decoded:
            raise ValueError
        return decoded
    except Exception:
        raise AgentLinkFailure(code) from None


def decrypt_v10_secret(encoded: str, key: bytes) -> str:
    """Authenticate the complete GCM ciphertext before decoding any plaintext."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED)
    encrypted = _decode_base64(encoded, LinkErrorCode.GROK_CONTRACT_CHANGED)
    if len(encrypted) < 3 + 12 + 16 or encrypted[:3] != b"v10":
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    try:
        return AESGCM(key).decrypt(encrypted[3:15], encrypted[15:], None).decode("utf-8")
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED) from None


def read_credentials(
    profile_reader: ProfileReader,
    dpapi_unprotector: DPAPIUnprotector,
    *,
    expected_account_identity: str | None = None,
    expected_team_identity: str | None = None,
) -> GrokCredentials:
    app_version = _app_version(profile_reader)
    pin = _account_pin(profile_reader)
    if expected_account_identity is not None and pin.active_identity != expected_account_identity:
        raise AgentLinkFailure(LinkErrorCode.GROK_ACCOUNT_CHANGED)
    local_state = _read_json(profile_reader, "Local State")
    try:
        wrapped = _decode_base64(
            local_state["os_crypt"]["encrypted_key"], LinkErrorCode.GROK_DPAPI_FAILED
        )
        if not wrapped.startswith(b"DPAPI") or len(wrapped) <= 5:
            raise ValueError
        storage_key = dpapi_unprotector.unprotect(wrapped[5:])
        if not isinstance(storage_key, bytes) or len(storage_key) != 32:
            raise ValueError
    except AgentLinkFailure as error:
        if error.code is LinkErrorCode.GROK_PROFILE_UNAVAILABLE:
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE) from None
        raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED) from None
    except Exception:
        raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED) from None
    team_id = (
        None if pin.team_ciphertext is None else decrypt_v10_secret(pin.team_ciphertext, storage_key)
    )
    if team_id is not None and (
        not re.fullmatch(r"[1-9][0-9]{0,15}", team_id)
        or int(team_id) > 9_007_199_254_740_991
    ):
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    if expected_team_identity is not None and (team_id or NO_SELECTED_TEAM) != expected_team_identity:
        raise AgentLinkFailure(LinkErrorCode.GROK_ACCOUNT_CHANGED)
    assert_account_pin(profile_reader, pin)
    access_token = decrypt_v10_secret(pin.access_token_ciphertext, storage_key)
    if not access_token.strip() or len(access_token) > 8192 or re.search(r"[\r\n\0]", access_token):
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    assert_account_pin(profile_reader, pin)
    return GrokCredentials(app_version, access_token, team_id, pin)
