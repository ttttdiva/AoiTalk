"""One explicit Windows connection diagnostic; source API, not EXE acceptance.

Uses the pinned packaged helper and product Edge/process ownership checks.
Never navigates, observes page contents, operates inputs, or retries connection.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

HELPER_CODES = frozenset({
    "outcome_unknown", "tab_gone", "rebind_pending", "connection_blocked",
    "connection_required", "STRICT_SESSION_REQUIRED", "STRICT_ACTION_UNSUPPORTED",
    "EXACT_TARGET_REQUIRED", "TARGET_SELECTION_REQUIRED", "target_unavailable",
    "invalid_endpoint", "unavailable", "BRIDGE_ACTION_FAILED", "PRIVATE_COMMAND_INVALID",
    "STALE_REF", "FOCUS_CHANGED", "STRICT_REFS_UNAVAILABLE", "REDACTION_UNAVAILABLE",
    "SAFE_SNAPSHOT_UNAVAILABLE", "SAFE_SNAPSHOT_REQUIRED", "STRICT_REF_REQUIRED",
    "STRICT_INPUT_UNSUPPORTED", "DIALOG_BLOCKED", "PRIVATE_INPUT_INVALID",
    "VERIFY_UNAVAILABLE", "VERIFY_INVALID", "CROWDWORKS_SCOPE_UNSUPPORTED",
    "CROWDWORKS_SCOPE_OVERFLOW", "CROWDWORKS_INSPECTION_UNAVAILABLE",
})
CONNECTION_DIAGNOSTIC_CODES = frozenset({
    "websocket_timeout", "websocket_connect_error",
    "target_discovery_timeout", "target_discovery_error",
    "target_metadata_timeout", "target_metadata_error",
})
ERROR_CODES = HELPER_CODES | frozenset({
    "agent_browser_command_blocked", "agent_browser_command_failed", "agent_browser_outcome_unknown",
    "agent_browser_preflight_failed", "agent_browser_bridge_contract_mismatch",
    "agent_browser_connection_blocked", "agent_browser_connection_required",
    "agent_browser_connection_unverified", "agent_browser_target_selection_required",
    "agent_browser_target_binding_failed", "agent_browser_target_changed", "agent_browser_targets_invalid",
    "agent_browser_target_mismatch", "agent_browser_target_gone", "agent_browser_rebind_pending",
    "agent_browser_timeout", "agent_browser_json_invalid", "agent_browser_output_limit",
    "agent_browser_spawn_failed", "agent_browser_cleanup_failed", "agent_browser_execution_failed",
    "agent_browser_daemon_ownership_lost", "agent_browser_config_changed", "agent_browser_config_unavailable",
    "agent_browser_initial_connect_required", "agent_browser_runtime_closed", "agent_browser_command_rejected",
    "agent_browser_manifest_invalid", "agent_browser_manifest_unavailable", "agent_browser_bundle_unavailable",
    "agent_browser_binary_path_invalid", "agent_browser_binary_size_mismatch", "agent_browser_binary_hash_mismatch",
    "agent_browser_binary_unavailable", "edge_listener_owner_unverified", "edge_listener_not_edge",
    "edge_listener_not_loopback", "edge_process_identity_unverified", "edge_process_identity_changed",
    "edge_listener_owner_changed", "edge_channel_path_mismatch", "edge_cdp_endpoint_invalid",
    "diagnostic_command_rejected", "diagnostic_input_invalid", "diagnostic_requires_windows",
})


def safe_error(exc):
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = str(exc)
    return code if code in ERROR_CODES else "unexported_error"


def response_fields(response):
    if not isinstance(response, dict):
        return {"success": None, "code": "invalid_response", "executed": None}
    data, code = response.get("data"), response.get("code")
    executed = data.get("executed") if isinstance(data, dict) else None
    result = {
        "success": response.get("success") if type(response.get("success")) is bool else None,
        "code": code if isinstance(code, str) and code in HELPER_CODES
        else None if code is None else "unexported_helper_code",
        "executed": executed if type(executed) is bool else None,
    }
    diagnostic = data.get("connectionDiagnostic") if isinstance(data, dict) else None
    if (response.get("success") is False and code == "unavailable"
            and isinstance(diagnostic, str) and diagnostic in CONNECTION_DIAGNOSTIC_CODES):
        result["connection_diagnostic"] = diagnostic
    return result


class RecordingRuntime:
    def __init__(self, runtime, record):
        self.runtime, self.record = runtime, record

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def preflight(self, **kwargs):
        return self._call("preflight", lambda: self.runtime.preflight(**kwargs), preflight=True)

    def _call(self, stage, call, *, preflight=False):
        row = {"stage": stage, "returned": False}
        self.record(row)
        try:
            response = call()
            row.update(returned=True, **({"success": True} if preflight else response_fields(response)))
            return response
        except Exception as exc:
            row.update(success=False, code=safe_error(exc), executed=None)
            raise
        finally:
            self.record(None)

    def run(self, args, **kwargs):
        args = tuple(args)
        if args == ("connect", self.runtime.endpoint.url):
            stage = "initial_connect"
        elif args == ("tab", "list"):
            stage = "tab_list"
        elif args == ("tab", self.runtime.attach_target) and self.runtime.attach_target is not None:
            stage = "target_select"
        elif args == ("bridge-command", "--stdin") and json.loads(kwargs.get("stdin_bytes", b"null")) == {
            "argv": ["tab", "list", "--local-labels"]
        }:
            stage = "local_target_list"
        else:
            raise RuntimeError("diagnostic_command_rejected")
        return self._call(stage, lambda: self.runtime.run(args, **kwargs))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("diagnostic_input_invalid")
        result[key] = value
    return result


def cleanup(bridge, runtimes, identities, work, *, inspector_factory, pid_exists):
    """Stop only owned helpers and retain scratch if their exit is unproven."""
    result = {"helper_job_empty": bridge is None, "owned_processes_exited": not runtimes,
              "existing_edge_identity_unchanged": None, "temporary_files_removed": False}
    records = None
    try:
        records = [identity for runtime in runtimes for identity in runtime.owned_processes]
    except Exception:
        pass
    if bridge is not None:
        try:
            result["helper_job_empty"] = bridge.close() is True
        except BaseException:
            result["helper_job_empty"] = False
    if runtimes and records is not None:
        try:
            inspector = inspector_factory()
            def exited(identity):
                current = inspector.process_identity(identity.pid)
                if current is not None:
                    return current.creation_ticks != identity.creation_ticks
                # None also means access/query failure, not necessarily exit.
                # Require an independent OS PID absence check in that case.
                return pid_exists(identity.pid) is False
            result["owned_processes_exited"] = all(exited(identity) for identity in records)
        except Exception:
            result["owned_processes_exited"] = False
    if identities:
        try:
            for verifier, endpoint, identity in identities:
                verifier.verify(endpoint, identity)
            result["existing_edge_identity_unchanged"] = True
        except Exception:
            result["existing_edge_identity_unchanged"] = False
    if result["helper_job_empty"] and result["owned_processes_exited"]:
        try:
            if work.exists():
                shutil.rmtree(work)
            result["temporary_files_removed"] = True
        except OSError:
            pass
    return result


def main(argv=None):
    del argv
    print(json.dumps({"success": False, "code": "direct_edge_cdp_forbidden"}, ensure_ascii=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
