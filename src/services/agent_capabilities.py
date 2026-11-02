"""Neutral Agent capability catalog shared by authority and runtimes.

Capabilities are policy identifiers, not Team topology.  Keeping this catalog
separate lets the Team/ExecutionProfile implementation retire independently.
"""

from __future__ import annotations

import copy
from typing import Any


AGENT_CAPABILITY_CATALOG: dict[str, dict[str, Any]] = {
    "workspace_read": {"id": "workspace_read", "family": "workspace", "access": "read", "native": True},
    "workspace_write": {"id": "workspace_write", "family": "workspace", "access": "write", "native": True},
    "repo_map": {"id": "repo_map", "family": "workspace", "access": "read", "native": True},
    "command_execute": {"id": "command_execute", "family": "workspace", "access": "write", "native": True},
    "aoi_tools": {"id": "aoi_tools", "family": "aoi", "access": "read", "native": False},
    "docs_read": {"id": "docs_read", "family": "docs", "access": "read", "native": False},
    "docs_write": {"id": "docs_write", "family": "docs", "access": "write", "native": False},
    "project_read": {"id": "project_read", "family": "project", "access": "read", "native": False},
    "project_write": {"id": "project_write", "family": "project", "access": "write", "native": False},
    "web_read": {"id": "web_read", "family": "web", "access": "read", "native": False, "shared": True},
    "browser_qa": {"id": "browser_qa", "family": "browser", "access": "write", "native": False, "parent_owned": True},
    "media": {"id": "media", "family": "media", "access": "write", "native": False},
    "spotify": {"id": "spotify", "family": "spotify", "access": "write", "native": False},
    "crowdworks": {
        "id": "crowdworks",
        "family": "crowdworks",
        "access": "write",
        "native": False,
        "description": "CrowdWorks の一覧・詳細の読取と、承認待ちの応募案の作成",
    },
    "external_action_propose": {"id": "external_action_propose", "family": "employee", "access": "write", "native": False},
    "remote_agent_delegate": {"id": "remote_agent_delegate", "family": "remote", "access": "write", "native": False},
    "personal_deadline_read": {"id": "personal_deadline_read", "family": "personal", "access": "read", "native": False},
    "personal_deadline_draft": {"id": "personal_deadline_draft", "family": "personal", "access": "read", "native": False},
    "personal_inventory_read": {"id": "personal_inventory_read", "family": "personal", "access": "read", "native": False},
    "personal_inventory_draft": {"id": "personal_inventory_draft", "family": "personal", "access": "read", "native": False},
    "personal_finance_read": {"id": "personal_finance_read", "family": "personal", "access": "read", "native": False},
    "personal_career_context": {"id": "personal_career_context", "family": "personal", "access": "read", "native": False},
}


def agent_capability_catalog() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(AGENT_CAPABILITY_CATALOG)


__all__ = ["AGENT_CAPABILITY_CATALOG", "agent_capability_catalog"]
