"""Explicit, single-connection Windows acceptance of the packaged Bridge helper.

This is a source-API acceptance runner, not an exe/GUI/WSS end-to-end test.
No browser launcher, direct CDP, downloads, settings writes, or action retries.
See docs/pc_bridge_windows_acceptance.md before using --live on Windows.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Only these known codes may be exported; arbitrary exception/helper text never is.
SAFE_ERRORS = frozenset({
    "bridge_command_expired", "bridge_command_cancelled", "bridge_busy",
    "edge_stale_element", "agent_browser_outcome_unknown", "agent_browser_timeout",
    "agent_browser_dialog_blocked", "agent_browser_permission_pending",
    "agent_browser_permission_unknown", "agent_browser_connection_blocked",
    "agent_browser_connection_required", "agent_browser_reconnect_required",
    "agent_browser_session_faulted", "agent_browser_run_required", "agent_browser_target_changed",
    "agent_browser_target_gone", "agent_browser_rebind_pending",
    "agent_browser_snapshot_invalid", "candidate_scope_required",
    "agent_browser_preflight_failed", "agent_browser_bundle_unavailable",
    "agent_browser_manifest_invalid", "agent_browser_manifest_unavailable",
    "agent_browser_binary_hash_mismatch", "agent_browser_binary_size_mismatch",
    "edge_process_identity_changed", "edge_listener_owner_unverified",
    "edge_listener_not_edge", "edge_listener_not_loopback",
    "edge_process_verification_requires_windows", "edge_cdp_endpoint_invalid",
    "edge_listener_owner_changed", "acceptance_expectation_failed", "operator_cancelled_before_connection",
})


class AcceptanceFailure(RuntimeError):
    """A fixed runner assertion, with details already durably projected to JSON."""


def require(condition):
    if not condition:
        raise AcceptanceFailure("acceptance_expectation_failed")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_traceback(exc):
    """Preserve frame/line evidence without exception text, source lines or locals."""
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            file = Path(frame.filename).resolve().relative_to(ROOT).as_posix()
            if not file.startswith(("pc_bridge/", "scripts/", "tests/")):
                file = "<external>"
        except ValueError:
            file = "<external>"
        frames.append({"file": file, "line": frame.lineno, "function": frame.name})
    code = str(exc)
    return {"type": type(exc).__name__,
            "code": code if code in SAFE_ERRORS
            else "unexported_exception_text", "frames": frames}


class Evidence:
    def __init__(self, directory, fixture, revision=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(__file__, self.directory / "runner.py")
        (self.directory / "fixture.html").write_text(fixture.html(), encoding="utf-8")
        # Resolve the entrypoint before relativising. The Windows checkout may
        # be reached through a junction (for example C:\\nk -> D:\\Dev), while
        # ``ROOT`` is intentionally the canonical path used in the evidence.
        sources = [Path(__file__).resolve(), *sorted((ROOT / "pc_bridge").glob("*.py"))]
        self.report = {
            "schema": 1, "status": "running", "mode": "source_api_packaged_helper",
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "declared_source_revision": revision,
            "source_sha256": {p.relative_to(ROOT).as_posix(): sha256(p) for p in sources},
            "runner_sha256": sha256(self.directory / "runner.py"),
            "fixture_sha256": sha256(self.directory / "fixture.html"),
            "synthetic": {"title": fixture.title, "scope": fixture.scope,
                          "input": fixture.value, "option": fixture.option},
            "privacy": "Only allowlisted result fields; no raw tab list, snapshot or exception text",
            "stages": [], "cleanup": {},
            "unverified": [
                "portable exe GUI and WSS",
                "JEV service and Chat same-device routing",
                "page click and Computer Use",
                "real accounts",
                "native security prompt detection",
            ],
        }
        self.flush()

    def flush(self):
        temporary = self.directory / "report.new"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "report.json")

    def stage(self, name, expected, call, project, check):
        row = {"stage": name, "expected": expected, "status": "started"}
        self.report["stages"].append(row)
        self.flush()  # A killed runner leaves the last attempted stage visible.
        try:
            value = call()
            row["actual"] = project(value)
            row["status"] = "returned"
            self.flush()  # Persist actual result BEFORE assertions/cleanup.
            check(value)
            row["status"] = "passed"
            return value
        except BaseException as exc:
            row["status"] = "failed"
            row["traceback"] = safe_traceback(exc)
            self.report["status"] = "failed"
            raise
        finally:
            self.flush()


@dataclass(frozen=True)
class Fixture:
    nonce: str
    origin: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[a-f0-9]{32}", self.nonce):
            raise ValueError("invalid_fixture_nonce")

    @property
    def title(self):
        return "Bridge acceptance " + self.nonce

    @property
    def scope(self):
        return {"selector": "#bridge-acceptance-" + self.nonce}

    @property
    def url(self):
        return self.origin + "/" + self.nonce + "/"

    @property
    def field(self):
        return "Synthetic input " + self.nonce

    @property
    def choice(self):
        return "Synthetic choice " + self.nonce

    @property
    def value(self):
        return "Bridge 日本語 " + self.nonce

    @property
    def option(self):
        return "synthetic-b"

    def html(self):
        # Accessible names are interactive evidence; heading/body text is not.
        return f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>{self.title}</title><h1>Synthetic Bridge acceptance</h1>
<form id="bridge-acceptance-{self.nonce}">
<label for="input">{self.field}</label><input id="input" autocomplete="off">
<label for="choice">{self.choice}</label><select id="choice">
<option value="synthetic-a">Synthetic A</option>
<option value="synthetic-b">Synthetic B</option></select></form></html>'''


class FixtureServer:
    def __init__(self, nonce):
        self.fixture = Fixture(nonce)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/" + nonce + "/":
                    self.send_error(404)
                    return
                data = owner.fixture.html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class BoundedServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(2)
                return connection, address

        self.server = BoundedServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 1
        self.fixture = Fixture(nonce, f"http://127.0.0.1:{self.server.server_port}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            raise

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        return not self.thread.is_alive()


def result_projection(value, fixture):
    """Never emit an unexpected origin, label, URL, key, value, ref or free text."""
    if not isinstance(value, dict):
        return {"is_object": False}
    output = {"is_object": True}
    for key in ("ok", "selected", "matched", "ended", "refs_invalidated", "connection_retained", "executed"):
        if key in value:
            item = value[key]
            output[key] = item if item is None or type(item) is bool else "invalid_type"
    if "error" in value:
        error = value["error"]
        output["error"] = error if isinstance(error, str) and error in SAFE_ERRORS else "unexported_error"
    if "execution_status" in value:
        status = value["execution_status"]
        output["execution_status"] = status if status in ("executed", "blocked", "unknown") else "invalid_status"
    if "result" in value:
        output["result"] = result_projection(value["result"], fixture)
    if "url" in value:
        output["origin_matches"] = value["url"] == fixture.origin
    if "tab_id" in value:
        output["tab_handle_is_integer"] = type(value["tab_id"]) is int
    if "coverage" in value:
        coverage = value["coverage"]
        output["coverage"] = {"complete": isinstance(coverage, dict) and coverage.get("complete") is True,
                              "scope_matches": isinstance(coverage, dict) and coverage.get("scope") == fixture.scope}
    if "candidates" in value:
        candidates = value["candidates"]
        output["candidate_count"] = len(candidates) if isinstance(candidates, dict) else None
        output["known_controls"] = [
            {"role": item["role"], "label": item["label"]}
            for item in candidates.values() if isinstance(item, dict)
            and isinstance(item.get("role"), str) and isinstance(item.get("label"), str)
            and (item.get("role"), item.get("label")) in
            {("textbox", fixture.field), ("combobox", fixture.choice),
             ("option", "Synthetic A"), ("option", "Synthetic B")}
        ] if isinstance(candidates, dict) else []
    return output


def snapshot_diagnostic(response, fixture):
    """Structural evidence only, from this runner's exact synthetic snapshot.

    This never returns raw names, values, refs, keys, origins or tree fragments.
    Reuse the product parser; do not introduce a permissive diagnostic grammar.
    """
    from pc_bridge.agent_browser_adapter import _parse_snapshot, _SnapshotBudget, _SnapshotInvalid

    if not isinstance(response, dict) or response.get("success") is not True:
        return {"status": "helper_unsuccessful"}
    data = response.get("data")
    if not isinstance(data, dict) or data.get("origin") != fixture.origin:
        return {"status": "suppressed_origin_unproven"}

    def value_type(key):
        if key not in data:
            return "missing"
        value = data[key]
        return ({str: "string", dict: "object", list: "array", bool: "boolean",
                 int: "number", float: "number", type(None): "null"}).get(type(value), "other")

    result = {"status": "invalid", "snapshot_type": value_type("snapshot"), "refs_type": value_type("refs")}
    tree, refs = data.get("snapshot"), data.get("refs")
    if not isinstance(tree, str) or not isinstance(refs, dict):
        result["reason"] = "envelope_types"
        return result
    try:
        size = len(tree.encode("utf-8"))
    except UnicodeError:
        result["reason"] = "tree_unicode"
        return result
    # These are the product's default complete-snapshot budgets, not prefixes.
    if size > 262144 or len(refs) > 4096:
        result.update(status="scope_required", reason="snapshot_budget_exceeded")
        return result
    result.update(tree_bytes=size, structured_ref_count=len(refs))
    try:
        nodes = _parse_snapshot(tree, refs, max_bytes=262144)
    except _SnapshotInvalid as exc:
        result["reason"] = exc.reason
        result["traceback"] = safe_traceback(exc)
    except _SnapshotBudget:
        result.update(status="scope_required", reason="projection_budget_exceeded")
    else:
        result.update(status="valid", node_count=len(nodes),
                      rendered_ref_count=sum(node["ref"] is not None for node in nodes),
                      redacted_option_count=sum(node["role"] == "option" and not node["label"] for node in nodes))
    return result


class SnapshotRecordingRuntime:
    """Observe one already-returned response, never issue/replay a helper call."""

    def __init__(self, runtime, fixture, evidence):
        self._runtime, self._fixture, self._evidence = runtime, fixture, evidence

    def __getattr__(self, name):
        return getattr(self._runtime, name)

    def run(self, args, **kwargs):
        response = self._runtime.run(args, **kwargs)
        expected = ("snapshot", "--interactive", "--redact-editable-values", "--selector", self._fixture.scope["selector"])
        if tuple(args) == expected:
            records = self._evidence.report.setdefault("snapshot_diagnostics", [])
            # The fixed acceptance flow has five snapshots; a bound protects any
            # accidental future loop without saving a prefix of a large page.
            require(len(records) < 8)
            records.append(snapshot_diagnostic(response, self._fixture))
            self._evidence.flush()
        return response


def own_targets(value, fixture):
    targets = value.get("targets") if isinstance(value, dict) else None
    return [item for item in targets if isinstance(item, dict) and
            item.get("origin") == fixture.origin and item.get("title") == fixture.title and
            item.get("type") == "page" and type(item.get("tab_id")) is int] if isinstance(targets, list) else []


def body(response):
    require(isinstance(response, dict) and response.get("ok") is True)
    require(isinstance(response.get("result"), dict))
    return response["result"]


def run_acceptance(client, fixture, evidence):
    """All browser operations use BridgeClient.setup/dispatch, on one exact form."""
    def stage(name, expected, call, check, project=None):
        return evidence.stage(name, expected, call,
                              project or (lambda v: result_projection(v, fixture)), check)

    discovered = stage("discover", "Exactly one synthetic origin/title match; no metadata exported",
                       lambda: client.browser_setup("discover_targets", {}),
                       lambda v: require(len(own_targets(v, fixture)) == 1),
                       lambda v: {"matching_fixture_targets": len(own_targets(v, fixture)),
                                  "target_count": len(v["targets"]) if isinstance(v, dict)
                                  and isinstance(v.get("targets"), list) else None})
    handle = own_targets(discovered, fixture)[0]["tab_id"]
    stage("select", "Selected the discovered synthetic handle",
          lambda: client.browser_setup("select_target", {"tab_id": handle}),
          lambda v: require(v.get("selected") is True and v.get("tab_id") == handle))
    run_id = "acceptance-" + fixture.nonce

    def dispatch(command, **params):
        return client.dispatch({"type": "command", "id": uuid4().hex, "channel": "browser",
                                "action": command, "expires_at": time.time() + 40,
                                "params": {"tab_id": handle, "run_id": run_id, **params}})

    resolved = body(stage("resolve", "Selected origin exactly equals the synthetic loopback origin",
                          lambda: dispatch("resolve_tab"),
                          lambda v: require(body(v).get("url") == fixture.origin)))
    epoch = resolved["epoch"]

    def observe(action):
        def check(response):
            value = body(response)
            require(value.get("url") == fixture.origin and value.get("tab_id") == handle)
            require(value.get("run_id") == run_id and value.get("epoch") == epoch)
            require(value.get("coverage", {}).get("complete") is True)
            require(value["coverage"].get("scope") == fixture.scope)
            require(isinstance(value.get("snapshot_id"), str) and value["snapshot_id"])
            roles = {"read": {("textbox", fixture.field), ("combobox", fixture.choice)},
                     "type": {("textbox", fixture.field)}, "select": {("combobox", fixture.choice)}}[action]
            found = [(v.get("role"), v.get("label")) for v in value.get("candidates", {}).values()]
            # Native AX trees may expose select options as interactive refs.
            # Keep both identity controls mandatory in the read projection.
            # bridge.4 redacts SELECT descendants, including option names.
            # Require each named control exactly once; blank options carry no
            # action authority and must not be mistaken for duplicate controls.
            options = [item for item in found if item[0] == "option"]
            controls = [item for item in found if item[0] != "option"]
            require(len(controls) == len(roles) and set(controls) == roles)
            require(not options or action == "read")
            require(len(options) <= 2 and all(name in {"", "Synthetic A", "Synthetic B"} for _, name in options))
            named_options = [name for _, name in options if name]
            require(len(named_options) == len(set(named_options)))
        return body(stage("snapshot_" + action, "Complete exact form scope and expected interactive names",
                          lambda: dispatch("observe", action=action, scope=fixture.scope), check))

    def ref(snapshot, label):
        return next(key for key, value in snapshot["candidates"].items() if value["label"] == label)

    stage("verify_url", "Helper locally verifies the full synthetic URL", lambda: dispatch("verify", expected_url=fixture.url),
          lambda v: require(body(v).get("matched") is True))
    observe("read")
    for action, label, value in (("type", fixture.field, fixture.value), ("select", fixture.choice, fixture.option)):
        snapshot = observe(action)
        authority = {"target": ref(snapshot, label), "snapshot_id": snapshot["snapshot_id"], "epoch": epoch}
        stage("fill" if action == "type" else "select_option", "Exactly one executed action; no unknown outcome",
              lambda: dispatch("act", action=action, value=value, **authority),
              lambda v: require(body(v).get("ok") is True and body(v).get("executed") is True
                                and body(v).get("execution_status") == "executed"))
        stage("verify_" + action, "Helper predicate matches the synthetic value without exporting field contents",
              lambda: dispatch("verify", expected_value=value, expected_url=fixture.url, **authority),
              lambda v: require(body(v).get("matched") is True))
    # Refresh twice, then deliberately use the older observation on our input.
    stale = observe("type")
    fresh = observe("type")
    stage("stale", "Old snapshot is blocked before execution",
          lambda: dispatch("act", action="type", value="MUST-NOT-BE-APPLIED-" + fixture.nonce,
                           target=ref(stale, fixture.field), snapshot_id=stale["snapshot_id"], epoch=epoch),
          lambda v: require(body(v).get("ok") is False and body(v).get("executed") is False
                            and body(v).get("error") == "edge_stale_element"
                            and body(v).get("execution_status") == "blocked"))
    stage("verify_after_stale", "Input is still the previously verified synthetic value",
          lambda: dispatch("verify", expected_value=fixture.value, expected_url=fixture.url,
                           target=ref(fresh, fixture.field), snapshot_id=fresh["snapshot_id"], epoch=epoch),
          lambda v: require(body(v).get("matched") is True))
    stage("end_run", "Refs invalidated, same approved connection retained",
          lambda: dispatch("end_run"),
          lambda v: require(body(v) == {"ended": True, "refs_invalidated": True, "connection_retained": True}))
    # A read-only post-run rejection proves authority ended without another run.
    stage("after_end_run", "Observe is rejected with run_required; no reconnect",
          lambda: dispatch("observe", action="read", scope=fixture.scope),
          lambda v: require(v.get("ok") is False and v.get("error") == "agent_browser_run_required"))
    stage("single_connection", "Exactly one connection attempt, still connected and selected",
          client.browser_diagnostics,
          lambda v: require(type(v.get("connect_attempts")) is int and v["connect_attempts"] == 1
                            and v.get("connected") is True and v.get("selected") is True),
          lambda v: {key: v.get(key) for key in ("connect_attempts", "connected", "selected", "blocked")})


def make_client(
    bundle_dir,
    endpoint,
    channel,
    work,
    runtimes,
    identities,
    *,
    fixture=None,
    evidence=None,
    require_pinned=False,
):
    from pc_bridge.agent_browser_runtime import OwnedHelperRuntime
    from pc_bridge.browser_backend import create_browser_backend
    from pc_bridge.client import BridgeClient
    from pc_bridge.windows_edge_identity import EdgeEndpointIdentityVerifier

    class RecordingVerifier(EdgeEndpointIdentityVerifier):
        def capture(self, endpoint):
            identity = super().capture(endpoint)
            identities.append((self, endpoint, identity))
            return identity

    def runtime_factory(**kwargs):
        # Product injection seam keeps credentials/endpoint scratch in .local.
        kwargs["state_directory"] = work / kwargs["session_id"]
        runtime = OwnedHelperRuntime(**kwargs)
        if fixture is not None and evidence is not None:
            runtime = SnapshotRecordingRuntime(runtime, fixture, evidence)
        runtimes.append(runtime)
        return runtime

    config = {"browser_backend": "agent_browser", "agent_browser": {"channel": channel, "endpoint": endpoint}}
    client = BridgeClient(config)
    require(client.edge.general.close() is True)  # This lazy, unused backend has no I/O.
    client.edge.general = create_browser_backend(config, cancel_event=client.operation_cancelled,
                                         bundle_directory=bundle_dir, runtime_factory=runtime_factory,
                                         edge_identity_verifier=RecordingVerifier(),
                                         require_pinned=require_pinned)
    return client


def cleanup_owned(client, server, work, runtimes, identities, *, inspector=None):
    """Attempt every independent cleanup even when another cleanup fails."""
    result = {"helper_job_empty": client is None, "server_stopped": server is None,
              "owned_processes_exited": not runtimes, "server_port_released": server is None,
              "existing_edge_identity_unchanged": None, "temporary_files_removed": False,
              "fixture_tab": "manual_close_required", "fixture_tab_closed": False,
              "errors": []}

    def attempt(name, call):
        try:
            result[name] = call() is True
        except BaseException as exc:
            result[name] = False
            result["errors"].append({"stage": name, "traceback": safe_traceback(exc)})

    records = []
    ownership_recorded = True
    try:
        records = [p for runtime in runtimes for p in runtime.owned_processes]
    except BaseException as exc:
        ownership_recorded = False
        result["errors"].append({"stage": "ownership_records", "traceback": safe_traceback(exc)})
    if client is not None:
        client.operation_cancelled.set()
        attempt("helper_job_empty", client.edge.close)
    if server is not None:
        attempt("server_stopped", server.close)
    if inspector is not None:
        def processes_exited():
            if not ownership_recorded:
                return False
            for record in records:
                current = inspector.process_identity(record.pid)
                if current is not None and current.creation_ticks == record.creation_ticks:
                    return False
            return True
        attempt("owned_processes_exited", processes_exited)
        if server is not None:
            attempt("server_port_released", lambda: inspector.listener_owners("127.0.0.1", server.server.server_port) == ())
    if identities:
        def edge_unchanged():
            for verifier, endpoint, identity in identities:
                verifier.verify(endpoint, identity)
            return True
        attempt("existing_edge_identity_unchanged", edge_unchanged)
    if all(result[key] for key in ("helper_job_empty", "server_stopped", "owned_processes_exited", "server_port_released")):
        def remove_work():
            shutil.rmtree(work)
            return True
        attempt("temporary_files_removed", remove_work)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly permit this one Windows acceptance session")
    parser.add_argument("--bundle-dir", type=Path, required=True, help="existing packaged resources/agent-browser directory")
    parser.add_argument("--endpoint", required=True, help="existing approved exact loopback Edge WebSocket endpoint; never logged")
    parser.add_argument("--channel", choices=("stable", "beta", "dev", "canary"), default="stable")
    parser.add_argument("--source-revision", type=lambda v: v if re.fullmatch(r"[a-f0-9]{40}", v) else parser.error("expected full source revision"))
    args = parser.parse_args(argv)
    if not args.live or os.name != "nt":
        parser.error("requires explicit --live on Windows; offline tests do not use this entry point")

    nonce = uuid4().hex
    work = ROOT / ".local" / "pc-bridge-acceptance" / nonce
    work.mkdir(parents=True, exist_ok=False)
    server = client = evidence = None
    runtimes, identities = [], []
    # create_browser_backend may make these empty directories before its factory.
    from pc_bridge.paths import data_dir
    common_dirs = [data_dir(), data_dir() / "agent-browser"]
    existing_dirs = {path for path in common_dirs if path.exists()}
    success = False
    try:
        server = FixtureServer(nonce)
        fixture = server.fixture
        evidence = Evidence(ROOT / "artifacts" / "pc-bridge-acceptance" / nonce, fixture, args.source_revision)
        evidence.report["synthetic"]["url"] = fixture.url
        evidence.flush()
        print("既存の対象 Edge に、この検証専用 URL を新しいタブで一度だけ開いてください:")
        print(fixture.url)
        print("他のタブは操作しません。新しい許可・セキュリティ画面が出たら承認せず Ctrl+C で停止してください。")
        if input("専用フォームが表示されたら RUN と入力（それ以外は中止）: ").strip() != "RUN":
            raise AcceptanceFailure("operator_cancelled_before_connection")
        from pc_bridge.browser_backend import load_helper_bundle
        bundle = load_helper_bundle(
            args.bundle_dir.resolve(), require_pinned=True
        )  # Hash check only, no execution.
        evidence.report["helper"] = {"sha256": bundle.manifest.sha256,
                                     "daemon_stamp": bundle.daemon_stamp}
        evidence.flush()
        client = make_client(args.bundle_dir.resolve(), args.endpoint, args.channel, work, runtimes, identities,
                             fixture=fixture, evidence=evidence, require_pinned=True)
        run_acceptance(client, fixture, evidence)
        success = True
    except BaseException as exc:
        if evidence is not None:
            evidence.report["failure"] = safe_traceback(exc)
            evidence.report["status"] = "failed"
            evidence.flush()
        else:
            print("開始前に失敗しました。例外本文はプライバシー保護のため出力しません。")
    finally:
        # Even evidence-storage failure must not skip process/server cleanup.
        try:
            if evidence:
                evidence.report["owned_helper_processes"] = [
                    {"pid": p.pid, "creation_ticks": p.creation_ticks}
                    for runtime in runtimes for p in runtime.owned_processes]
                evidence.report["cleanup"] = {"status": "started"}
                evidence.flush()
        except BaseException:
            success = False
        from pc_bridge.windows_edge_identity import WindowsEdgeInspector
        try:
            inspector = WindowsEdgeInspector()
        except Exception:
            inspector = None  # Missing OS proof cannot produce PASS.
        cleanup = cleanup_owned(client, server, work, runtimes, identities, inspector=inspector)
        for path in reversed(common_dirs):
            if path not in existing_dirs and path.is_dir():
                try:
                    path.rmdir()  # Never remove preexisting directories or nonempty data.
                except OSError:
                    pass
        owned_cleanup_verified = all(cleanup[key] is True for key in (
            "helper_job_empty", "server_stopped", "owned_processes_exited", "server_port_released",
            "existing_edge_identity_unchanged", "temporary_files_removed"))
        if owned_cleanup_verified:
            print("helper/server cleanup is verified. Close only the synthetic acceptance tab, then type CLOSED.")
            try:
                confirmation = input("synthetic tab closed? [CLOSED/STOP]: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            cleanup["fixture_tab_closed"] = confirmation == "CLOSED"
            cleanup["fixture_tab_confirmation"] = (
                "operator_confirmed" if cleanup["fixture_tab_closed"] else "pending"
            )
        success = success and owned_cleanup_verified and cleanup["fixture_tab_closed"]
        if evidence:
            evidence.report["cleanup"] = cleanup
            evidence.report["status"] = (
                "passed" if success
                else "cleanup_pending" if owned_cleanup_verified
                else "failed"
            )
            try:
                evidence.flush()
            except Exception:
                print("最終証跡の保存に失敗しました。保存済み stage と cleanup 状態の確認が必要です。")
                success = False
            label = "PASS" if success else "CLEANUP_PENDING" if owned_cleanup_verified else "STOP/FAIL"
            print(label + ": " + str(evidence.directory / "report.json"))
            if not cleanup["fixture_tab_closed"]:
                print("検証専用タブは未確認のため、report.jsonのcleanupを先に確認してください。")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
