"""Shared project-membership permission normalization.

Permission data is persisted as JSON, but old or externally-created rows may
contain ``NULL``, an invalid JSON string, or another unexpected value.  Such a
row must not become an implicit grant in one service while being denied by
another service.
"""

from __future__ import annotations

import json
from typing import Any


PROJECT_PERMISSION_KEYS = frozenset(
    {"read", "write", "delete", "manage_members", "manage_settings"}
)
PROJECT_MEMBER_DEFAULT_PERMISSIONS: dict[str, dict[str, bool]] = {
    "owner": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": True,
    },
    "admin": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": True,
    },
    "member": {
        "read": True,
        "write": False,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
    "viewer": {
        "read": True,
        "write": False,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
}


def normalize_project_member_role(role: Any) -> str:
    """Return a supported persisted role or reject it without a fallback."""

    normalized = role.strip().lower() if isinstance(role, str) else ""
    if normalized not in PROJECT_MEMBER_DEFAULT_PERMISSIONS:
        raise ValueError("Unsupported project member role")
    return normalized


def get_default_project_permissions(role: Any) -> dict[str, bool]:
    """Return an isolated copy of the deny-by-default role policy."""

    return dict(PROJECT_MEMBER_DEFAULT_PERMISSIONS[normalize_project_member_role(role)])


def normalize_project_member_permissions(permissions: Any) -> dict[str, bool]:
    """Return a valid explicit ACL, or deny-all for malformed data.

    Membership ACLs are an allow-list with a closed schema.  Silently
    dropping an unknown key (or coercing a non-boolean value) would make one
    authorization path interpret the same persisted row differently from
    another, so any schema violation invalidates the whole ACL.
    """

    parsed: Any = permissions
    if isinstance(permissions, str):
        try:
            parsed = json.loads(permissions)
        except json.JSONDecodeError:
            parsed = None

    if not isinstance(parsed, dict):
        return {}
    if any(
        key not in PROJECT_PERMISSION_KEYS or not isinstance(value, bool)
        for key, value in parsed.items()
    ):
        return {}
    return dict(parsed)


def has_effective_project_permission(
    *,
    user_id: Any,
    user_role: Any,
    project_owner_id: Any,
    member_permissions: Any,
    permission: str,
) -> bool:
    """Apply the shared owner/global-admin policy, then explicit membership ACL."""

    if permission not in PROJECT_PERMISSION_KEYS:
        return False
    is_owner = (
        user_id is not None
        and project_owner_id is not None
        and str(user_id) == str(project_owner_id)
    )
    if str(user_role or "").lower() == "admin" or is_owner:
        return True
    return normalize_project_member_permissions(member_permissions).get(permission) is True
