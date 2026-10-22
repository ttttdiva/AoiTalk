"""Read-only Windows Edge policy observations, never effective permissions.

Only four mandatory scalar values are read from the current user's and machine
hives. SOFTWARE\\Policies and HKCU\\SOFTWARE are WOW64 shared keys; do not
invent a second source under Wow6432Node. Cloud precedence, browser refresh,
profile applicability and URL exceptions remain unverified.

References:
https://learn.microsoft.com/en-us/windows/win32/winprog64/shared-registry-keys
https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/RemoteDebuggingAllowed
https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/DeveloperToolsAvailability
https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/EdgeManagementPolicyOverridesPlatformPolicy
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal, TypedDict

POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Edge"
PolicyName = Literal[
    "RemoteDebuggingAllowed",
    "DeveloperToolsAvailability",
    "EdgeManagementPolicyOverridesPlatformPolicy",
    "EdgeManagementUserPolicyOverridesCloudMachinePolicy",
]
POLICY_NAMES: tuple[PolicyName, ...] = (
    "RemoteDebuggingAllowed",
    "DeveloperToolsAvailability",
    "EdgeManagementPolicyOverridesPlatformPolicy",
    "EdgeManagementUserPolicyOverridesCloudMachinePolicy",
)
HiveName = Literal["HKLM", "HKCU"]
ObservationState = Literal[
    "valid", "missing", "invalid_type", "invalid_value", "access_denied",
    "read_error", "unsupported_platform",
]
PolicyStatus = Literal[
    "deny_observed", "allow_observed", "restricted_observed",
    "extension_restricted_observed", "conflict", "not_observed", "invalid",
    "unreadable",
]
_HIVES: tuple[tuple[HiveName, str], ...] = (
    ("HKLM", "HKEY_LOCAL_MACHINE"), ("HKCU", "HKEY_CURRENT_USER"),
)
_REG_DWORD = 4
_DWORD_MAX = 0xFFFFFFFF


class EdgePolicySummary(TypedDict):
    effective_policy: Literal["unknown"]
    remote_debugging: PolicyStatus
    developer_tools: PolicyStatus
    conflicts: bool
    partial_read: bool
    cloud_precedence: Literal["not_resolved"]
    url_scope: Literal["not_observed"]
    edge_process_presence: Literal["unknown"]
    platform: Literal["windows", "unsupported"]


@dataclass(frozen=True)
class EdgePolicyObservation:
    """Fixed source identity and bounded integers; never retain raw strings."""

    policy: PolicyName
    hive: HiveName
    state: ObservationState
    registry_type: int | None = None
    integer_value: int | None = None


@dataclass(frozen=True)
class EdgePolicySnapshot:
    observations: tuple[EdgePolicyObservation, ...]
    platform: Literal["windows", "unsupported"]

    def _status(self, policy: PolicyName) -> PolicyStatus:
        rows = tuple(row for row in self.observations if row.policy == policy)
        values = {row.integer_value for row in rows if row.state == "valid"}
        if len(values) > 1:
            return "conflict"
        if values:
            value = next(iter(values))
            if policy == "RemoteDebuggingAllowed":
                return "deny_observed" if value == 0 else "allow_observed"
            if value == 0:
                return "extension_restricted_observed"
            return "restricted_observed" if value == 2 else "allow_observed"
        if any(row.state in {"access_denied", "read_error", "unsupported_platform"}
               for row in rows):
            return "unreadable"
        if any(row.state in {"invalid_type", "invalid_value"} for row in rows):
            return "invalid"
        return "not_observed"

    def summary(self) -> EdgePolicySummary:
        """A fresh bounded projection, with no registry or browser I/O.

        A valid observation survives an unreadable/invalid other hive, with
        partial_read true. A conflict means different valid values were seen,
        not that the browser could not resolve its policy sources.
        """
        conflicts = any(
            len({row.integer_value for row in self.observations
                 if row.policy == name and row.state == "valid"}) > 1
            for name in POLICY_NAMES
        )
        return {
            "effective_policy": "unknown",
            "remote_debugging": self._status("RemoteDebuggingAllowed"),
            "developer_tools": self._status("DeveloperToolsAvailability"),
            "conflicts": conflicts,
            "partial_read": any(row.state not in {"valid", "missing"}
                                for row in self.observations),
            "cloud_precedence": "not_resolved",
            "url_scope": "not_observed",
            "edge_process_presence": "unknown",
            "platform": self.platform,
        }


def _integer(value) -> int | None:
    return value if type(value) is int and 0 <= value <= _DWORD_MAX else None


def _error_state(exc: Exception) -> ObservationState:
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
        return "access_denied"
    return "read_error"


def _hive_state(hive: HiveName, state: ObservationState):
    return tuple(EdgePolicyObservation(name, hive, state) for name in POLICY_NAMES)


def _read_value(registry, key, hive: HiveName, name: PolicyName):
    try:
        value, kind = registry.QueryValueEx(key, name)
    except Exception as exc:
        return EdgePolicyObservation(name, hive, _error_state(exc))
    integer, registry_type = _integer(value), _integer(kind)
    if registry_type != _REG_DWORD:
        state: ObservationState = "invalid_type"
    elif integer is None or integer not in (
        (0, 1, 2) if name == "DeveloperToolsAvailability" else (0, 1)
    ):
        state = "invalid_value"
    else:
        state = "valid"
    return EdgePolicyObservation(name, hive, state, registry_type, integer)


def read_edge_policy_snapshot(registry=None, *, windows=None) -> EdgePolicySnapshot:
    """Observe eight fixed values once; no writes, probes or permission claims.

    windows/registry are test injection, never remote configuration. Non-Windows
    hosts do not import winreg or call an injected registry. Key handles close
    even when a value is malformed, absent or denied. Missing values are not
    false, and failure in one hive never prevents reading the other hive.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows is not True:
        return EdgePolicySnapshot(
            tuple(row for hive, _ in _HIVES
                  for row in _hive_state(hive, "unsupported_platform")),
            "unsupported",
        )
    if registry is None:
        try:
            import winreg as registry
        except ImportError:
            return EdgePolicySnapshot(
                tuple(row for hive, _ in _HIVES
                      for row in _hive_state(hive, "read_error")),
                "windows",
            )
    observations = []
    for hive, attribute in _HIVES:
        try:
            with registry.OpenKey(
                getattr(registry, attribute), POLICY_KEY, 0, registry.KEY_QUERY_VALUE
            ) as key:
                rows = tuple(_read_value(registry, key, hive, name)
                             for name in POLICY_NAMES)
        except Exception as exc:
            rows = _hive_state(hive, _error_state(exc))
        observations.extend(rows)
    return EdgePolicySnapshot(tuple(observations), "windows")
