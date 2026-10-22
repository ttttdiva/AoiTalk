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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize one Windows connection diagnostic")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True,
                        help="Local JSON: endpoint, channel, optional already-selected target_id; never copied to output")
    args = parser.parse_args(argv)
    if not args.live or os.name != "nt":
        parser.error("requires explicit --live on Windows")
    sys.path.insert(0, str(ROOT))
    from pc_bridge import browser_backend
    from pc_bridge.agent_browser_runtime import OwnedHelperRuntime
    from pc_bridge.windows_edge_identity import EdgeEndpointIdentityVerifier, WindowsEdgeInspector
    import psutil

    output = ROOT / ".local" / ("bridge-connect-diagnostic-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    work = output / "state"
    report = {"mode": "source_api_pinned_helper", "stages": [], "success": False}

    def record(row):
        if row is not None:
            report["stages"].append(row)
        temporary = output / "report.new"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output / "report.json")

    bridge = None
    runtimes, identities = [], []
    original_data_dir = browser_backend.data_dir

    class RecordingVerifier(EdgeEndpointIdentityVerifier):
        def capture(self, endpoint):
            identity = super().capture(endpoint)
            identities.append((self, endpoint, identity))
            return identity

    try:
        with args.input.open("rb") as stream:
            raw = stream.read(4097)
        settings = json.loads(raw, object_pairs_hook=unique_object) if len(raw) <= 4096 else None
        if (not isinstance(settings, dict) or not {"endpoint", "channel"} <= set(settings)
                or set(settings) - {"endpoint", "channel", "target_id"}
                or any(not isinstance(value, str) or not value for value in settings.values())):
            raise ValueError("diagnostic_input_invalid")
        work.mkdir()
        # This isolated process uses scratch only, never saved registration settings.
        browser_backend.data_dir = lambda: work

        def factory(**kwargs):
            kwargs["state_directory"] = work / kwargs["session_id"]
            report["discover_existing"] = kwargs["discover_existing"] is True
            report["explicit_target"] = kwargs["attach_target"] is not None
            record(None)
            runtime = OwnedHelperRuntime(**kwargs)
            runtimes.append(runtime)
            return RecordingRuntime(runtime, record)

        bridge = browser_backend.create_browser_backend(
            {"browser_backend": "agent_browser", "agent_browser": settings},
            bundle_directory=args.bundle_dir.resolve(), require_pinned=True, runtime_factory=factory,
            edge_identity_verifier=RecordingVerifier(),
        )
        if "target_id" in settings:
            asyncio.run(bridge.request("resolve_tab", tab_id=None, url="", run_id="connect-diagnostic"))
        else:
            bridge.local_setup("discover_targets", {})
        report["success"] = True
    except BaseException as exc:
        report.update(success=False, code=safe_error(exc))
    finally:
        browser_backend.data_dir = original_data_dir
        report["cleanup"] = cleanup(bridge, runtimes, identities, work,
                                    inspector_factory=WindowsEdgeInspector, pid_exists=psutil.pid_exists)
        report["connection_succeeded"] = report["success"]
        report["success"] = report["success"] and all(value is True for value in report["cleanup"].values())
        try:
            record(None)
        except Exception:
            report.update(success=False, evidence_saved=False)
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
