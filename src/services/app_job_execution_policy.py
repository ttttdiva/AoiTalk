"""Server-side App Job execution authorization.

Manifest/source edit permissions (developer/maintainer) are intentionally
separate from the operator contract required to execute arbitrary commands on
the AoiTalk host.  This module centralizes that gate so API routes, tools, and
the durable job worker share one decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


class ServerJobExecutionDenied(PermissionError):
    """Caller is not allowed to start a server-side App Job."""


@dataclass(frozen=True)
class ServerJobExecutionPolicy:
    """Operator-controlled contract for host-side App Job execution."""

    enabled: bool
    require_system_admin: bool


def _apps_jobs_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        config = {}
    apps = config.get("apps")
    if not isinstance(apps, dict):
        apps = {}
    jobs = apps.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    return jobs


def resolve_server_job_execution_policy(
    config: Mapping[str, Any] | None = None,
) -> ServerJobExecutionPolicy:
    """Resolve the deployment policy from config with env overrides.

    Defaults are fail-closed: server jobs are disabled until an operator
    explicitly enables them.
    """
    jobs = _apps_jobs_config(config)
    enabled_raw = os.environ.get("AOITALK_APP_JOBS_SERVER_EXECUTION_ENABLED")
    if enabled_raw is not None:
        enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        enabled = bool(jobs.get("server_execution_enabled", False))

    admin_raw = os.environ.get("AOITALK_APP_JOBS_REQUIRE_SYSTEM_ADMIN")
    if admin_raw is not None:
        require_system_admin = admin_raw.strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
    else:
        require_system_admin = bool(jobs.get("require_system_admin", True))

    return ServerJobExecutionPolicy(
        enabled=enabled,
        require_system_admin=require_system_admin,
    )


def user_may_start_server_job(
    *,
    user_role: str | None,
    policy: ServerJobExecutionPolicy | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    resolved = policy or resolve_server_job_execution_policy(config)
    if not resolved.enabled:
        return False
    if resolved.require_system_admin:
        return str(user_role or "") == "admin"
    return True


def assert_user_may_start_server_job(
    *,
    user_role: str | None,
    policy: ServerJobExecutionPolicy | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    resolved = policy or resolve_server_job_execution_policy(config)
    if not resolved.enabled:
        raise ServerJobExecutionDenied(
            "この環境ではサーバー上の App Job 実行が無効です。"
            " 運用者が server_execution を有効化するまで実行できません。"
        )
    if resolved.require_system_admin and str(user_role or "") != "admin":
        raise ServerJobExecutionDenied(
            "サーバー上の App Job 実行にはシステム管理者権限が必要です。"
            " App の編集権限だけではホスト上のコード実行はできません。"
        )


__all__ = [
    "ServerJobExecutionDenied",
    "ServerJobExecutionPolicy",
    "assert_user_may_start_server_job",
    "resolve_server_job_execution_policy",
    "user_may_start_server_job",
]
