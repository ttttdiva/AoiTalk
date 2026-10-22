"""Create and validate immutable Mobile release evidence.

The APK itself is the release payload.  This sidecar binds it to the exact
clean source snapshot, Mobile release gate, installer checks, and signer.  A
separate QA approval marker is required before publish-only can upload it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any


SCHEMA = "aoitalk-mobile-artifact/v1"
QA_SCHEMA = "aoitalk-mobile-qa-approval/v1"
QA_BUILD_SCHEMA = "aoitalk-mobile-qa-build/v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
SOURCE_INPUT_PATHS = (
    "mobile",
    "resources/chat-background",
    "resources/seethrough-rig/runtime",
    "scripts/build_apk.bat",
    "scripts/build_apk.sh",
    "scripts/build_mobile_apk.ps1",
    "scripts/check_mobile_release_gate.ps1",
    "scripts/mobile_release_manifest.py",
    "scripts/publish_public.ps1",
    "scripts/publish_latest_json.ps1",
    "scripts/stage_chat_background.py",
    "scripts/stage_mobile_runtime.py",
    "scripts/verify_mobile_apk.ps1",
    "Publish.bat",
)
COMPILED_INPUT_PATHS = (
    "mobile",
    "resources/chat-background",
    "resources/seethrough-rig/runtime",
    "scripts/build_apk.bat",
    "scripts/build_apk.sh",
    "scripts/build_mobile_apk.ps1",
    "scripts/stage_chat_background.py",
    "scripts/stage_mobile_runtime.py",
)

MOBILE_HASH_INPUT_PATHS = (
    "mobile",
    "resources/chat-background",
)


class ManifestError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ManifestError(message)


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if completed.returncode:
        detail = (completed.stdout + completed.stderr).strip()
        fail(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return completed.stdout


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args]).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_path(root: Path, value: str, *, strict: bool = True) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=strict)
    except OSError as exc:
        fail(f"artifact path cannot be resolved: {value}: {exc}")
    root_resolved = root.resolve(strict=True)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        fail(f"artifact path escaped project root: {value}")
    return resolved


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as exc:
        fail(f"path is outside project root: {path}")
        raise AssertionError from exc


def assert_hex(value: str, pattern: re.Pattern[str], label: str) -> str:
    normalized = str(value).strip().lower()
    if not pattern.fullmatch(normalized):
        fail(f"{label} is not a valid hexadecimal digest: {value}")
    return normalized


def strict_string(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value:
        fail(f"{label} must be a non-empty JSON string")
    if pattern is not None and not pattern.fullmatch(value):
        fail(f"{label} has an invalid format: {value}")
    return value


def strict_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        fail(f"{label} must be a non-negative JSON integer")
    return value


def strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        fail(f"{label} must be a JSON boolean")
    return value


def source_state(root: Path) -> tuple[str, str]:
    head = assert_hex(git(root, "rev-parse", "HEAD"), HEX40, "source HEAD")
    origin = assert_hex(
        git(root, "rev-parse", "refs/remotes/origin/main"), HEX40, "fixed origin/main revision"
    )
    if head != origin:
        fail(f"source HEAD does not equal fixed origin/main: {head} != {origin}")
    input_status = git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *SOURCE_INPUT_PATHS,
    )
    if input_status:
        fail(
            "fixed-origin Mobile build inputs or verifier sources are dirty; "
            f"refusing to bind an artifact:\n{input_status}"
        )
    return head, origin


def qa_build_requested() -> bool:
    enabled = os.environ.get("AOITALK_MOBILE_QA_BUILD", "").strip()
    branch = os.environ.get("AOITALK_MOBILE_QA_BRANCH", "").strip()
    revision = os.environ.get("AOITALK_MOBILE_QA_SOURCE_REVISION", "").strip()
    if enabled not in ("", "1"):
        fail("AOITALK_MOBILE_QA_BUILD must be explicitly set to 1 or unset")
    if enabled != "1":
        if branch or revision:
            fail("QA branch/source options require AOITALK_MOBILE_QA_BUILD=1")
        return False
    if os.environ.get("AOITALK_BUILD_ONLY", "") != "1":
        fail("QA branch-build requires AOITALK_BUILD_ONLY=1; publishing is forbidden")
    if not branch or not revision:
        fail("QA branch-build requires an explicit branch and full source revision")
    assert_hex(revision, HEX40, "QA expected source revision")
    return True


def build_source_state(root: Path) -> tuple[str, str, str | None]:
    if not qa_build_requested():
        head, origin = source_state(root)
        return head, origin, None
    branch = os.environ["AOITALK_MOBILE_QA_BRANCH"].strip()
    expected = assert_hex(os.environ["AOITALK_MOBILE_QA_SOURCE_REVISION"], HEX40, "QA expected source revision")
    git(root, "check-ref-format", "--branch", branch)
    if branch == "main" or git(root, "branch", "--show-current") != branch:
        fail("QA source must be the explicitly named candidate branch, not main or detached HEAD")
    head = assert_hex(git(root, "rev-parse", "HEAD"), HEX40, "QA source HEAD")
    origin = assert_hex(git(root, "rev-parse", "refs/remotes/origin/main"), HEX40, "actual origin/main revision")
    if head != expected:
        fail("QA source HEAD differs from the explicit expected source revision")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        fail("QA candidate checkout must be clean, including untracked files")
    ref = f"refs/heads/{branch}"
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "ls-remote", "--exit-code", "--heads", "origin", ref],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("QA origin branch could not be verified within 30 seconds")
    if remote.returncode:
        fail("QA candidate branch is not available on origin")
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if rows != [[head, ref]]:
        fail("QA candidate branch must match the actual origin SHA; push it before building")
    return head, origin, branch


def reject_qa_release_marker(apk: Path) -> None:
    marker = qa_marker_path_for(apk)
    if marker.exists() or marker.is_symlink():
        fail("QA branch-build artifacts cannot have a release approval marker")


def mobile_input_hashes(root: Path) -> dict[str, str]:
    entries = [
        value
        for value in git(root, "ls-files", "-z", "--", *MOBILE_HASH_INPUT_PATHS).split("\0")
        if value
    ]
    if not entries:
        fail("no tracked mobile build inputs were found")
    result: dict[str, str] = {}
    root_resolved = root.resolve(strict=True)
    for entry in entries:
        path = root / Path(entry)
        if path.is_symlink():
            fail(f"tracked mobile build input must not be a symlink/reparse point: {entry}")
        cursor = path
        while cursor != root:
            try:
                stat_result = cursor.lstat()
            except OSError as exc:
                fail(f"tracked mobile build input cannot be inspected: {entry}: {exc}")
            attributes = getattr(stat_result, "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                fail(f"tracked mobile build input has a reparse ancestor: {entry}")
            cursor = cursor.parent
        resolved = path.resolve(strict=True)
        if root_resolved not in resolved.parents:
            fail(f"tracked mobile build input escaped the project root: {entry}")
        if not path.is_file():
            fail(f"tracked mobile build input is missing: {entry}")
        result[entry.replace("\\", "/")] = sha256_file(path)
    return result


def assert_recorded_source_is_usable(root: Path, recorded_revision: str, current_origin: str) -> None:
    assert_hex(recorded_revision, HEX40, "recorded source revision")
    assert_hex(current_origin, HEX40, "current origin/main revision")
    git(root, "cat-file", "-e", f"{recorded_revision}^{{commit}}")
    git(root, "merge-base", "--is-ancestor", recorded_revision, current_origin)
    changes = [
        value for value in git(root, "diff", "--name-only", f"{recorded_revision}..{current_origin}", "--", *COMPILED_INPUT_PATHS).splitlines()
        if value
    ]
    if changes:
        fail("compiled Mobile inputs changed after the recorded build source:\n" + "\n".join(changes))


def read_app_config(root: Path) -> tuple[str, int, str]:
    path = root / "mobile" / "app.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        expo = config["expo"]
        version = strict_string(expo["version"], "mobile/app.json expo.version")
        version_code = expo["android"]["versionCode"]
        package = strict_string(expo["android"]["package"], "mobile/app.json android.package", PACKAGE_RE)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(f"mobile/app.json is invalid: {exc}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail("mobile/app.json has invalid version, versionCode, or package")
    if type(version_code) is not int or isinstance(version_code, bool) or not (1 <= version_code <= 2_147_483_647):
        fail("mobile/app.json has invalid version, versionCode, or package")
    return version, version_code, package


def app_at_revision(root: Path, revision: str) -> dict[str, Any]:
    try:
        raw = git(root, "show", f"{revision}:mobile/app.json")
        return json.loads(raw)["expo"]
    except (ManifestError, KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"mobile/app.json cannot be read at {revision}: {exc}")
    raise AssertionError


def release_gate(root: Path, base: str | None, target: str) -> dict[str, Any]:
    if not base:
        fail("AOITALK_MOBILE_RELEASE_BASE is required to create a publishable artifact manifest")
    base_revision = assert_hex(git(root, "rev-parse", base), HEX40, "release gate base")
    files = [value for value in git(root, "diff", "--name-only", f"{base_revision}..{target}").splitlines() if value]
    gate_script = root / "scripts" / "check_mobile_release_gate.ps1"
    gate_output = run(
        [
            powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(gate_script),
            "-Base",
            base_revision,
            "-Target",
            target,
        ],
        cwd=root,
    )
    values: dict[str, str] = {}
    for line in gate_output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if values.get("BASE", "").lower() != base_revision or values.get("TARGET", "").lower() != target:
        fail("canonical Mobile release gate returned a different base/target")
    mobile_changed = values.get("MOBILE_CHANGED") == "True"
    required = values.get("RELEASE_REQUIRED") == "True"
    version_bump_valid = values.get("VERSION_BUMP_VALID", "True") == "True"
    if required and not version_bump_valid:
        fail(values.get("VERSION_BUMP_ERROR", "canonical Mobile release gate rejected the version bump"))
    return {
        "base": base_revision,
        "target": target,
        "mobile_changed": mobile_changed,
        "release_required": required,
        "version_bump_valid": version_bump_valid,
        "changed_files": files,
    }


def powershell_executable() -> str:
    for name in ("powershell.exe", "pwsh.exe", "pwsh", "powershell"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    fail("PowerShell is required for the canonical APK verification function")
    raise AssertionError


def verify_with_canonical_script(root: Path, apk: Path, version: str, version_code: int) -> dict[str, Any]:
    verifier = root / "scripts" / "verify_mobile_apk.ps1"
    output = run(
        [
            powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(verifier),
            "-ApkPath",
            str(apk),
            "-ExpectedVersion",
            version,
            "-ExpectedVersionCode",
            str(version_code),
        ],
        cwd=root,
    )
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if values.get("APK_VERIFIED") != "True":
        fail("canonical verify_mobile_apk.ps1 did not report APK_VERIFIED=True")
    if values.get("APK_INSTALLER_MODULE") != "True" or values.get("REQUEST_INSTALL_PACKAGES") != "True":
        fail("canonical APK installer checks did not pass")
    return {
        "apk_installer_module": True,
        "request_install_packages": True,
        "version": values.get("APK_VERSION", ""),
        "version_code": int(values.get("APK_VERSION_CODE", "0")),
    }


def android_tool(root_name: str, filename: str) -> str:
    roots = []
    for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Android" / "Sdk")
    for root in roots:
        build_tools = root / "build-tools"
        if build_tools.is_dir():
            versions = sorted((path for path in build_tools.iterdir() if path.is_dir()), reverse=True)
            for version in versions:
                candidate = version / filename
                if candidate.is_file():
                    return str(candidate)
        candidate = root / "cmdline-tools" / "latest" / "bin" / filename
        if candidate.is_file():
            return str(candidate)
    fail(f"Android SDK tool was not found: {filename}")
    raise AssertionError


def run_android_tool(tool: str, args: list[str]) -> str:
    command = [tool, *args]
    if tool.lower().endswith(".bat"):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *command]
    return run(command)


def signature_evidence(apk: Path) -> dict[str, Any]:
    output = run_android_tool(android_tool("build-tools", "apksigner.bat"), ["verify", "--verbose", "--print-certs", str(apk)])
    v2_match = re.search(
        r"Verified using (?:v2 scheme \(APK Signature Scheme v2\)|APK Signature Scheme v2):\s*(true|false)",
        output,
        re.I,
    )
    signer_match = re.search(r"Number of signers:\s*(\d+)", output, re.I)
    cert_match = re.search(r"Signer #1 certificate SHA-256 digest:\s*([0-9a-f]{64})", output, re.I)
    if not v2_match or v2_match.group(1).lower() != "true":
        fail("APK v2 signature verification did not pass")
    if not signer_match or signer_match.group(1) != "1":
        fail("APK must contain exactly one signer")
    if not cert_match:
        fail("APK signer certificate SHA-256 was not reported")
    return {
        "scheme": "v2",
        "signer_count": 1,
        "certificate_sha256": cert_match.group(1).lower(),
    }


def manifest_path_for(apk: Path) -> Path:
    return Path(str(apk) + ".manifest.json")


def qa_marker_path_for(apk: Path) -> Path:
    return Path(str(apk) + ".qa-approved.json")


def assert_regular_sidecar(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        fail(f"sidecar must be a regular file and must not be a link: {path}")
    resolved = path.resolve(strict=True)
    if resolved.parent != path.parent.resolve(strict=True):
        fail(f"sidecar resolved outside its APK directory: {path}")


def invalidate_sidecars(apk: Path) -> None:
    for path in (manifest_path_for(apk), qa_marker_path_for(apk)):
        assert_regular_sidecar(path)
        if path.exists():
            path.unlink()


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    assert_regular_sidecar(path)
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"immutable artifact manifest is unreadable: {path}: {exc}")
        if not isinstance(existing, dict):
            fail(f"immutable artifact manifest must be a JSON object: {path}")
        comparable_existing = dict(existing)
        comparable_payload = dict(payload)
        comparable_existing.pop("created_at", None)
        comparable_payload.pop("created_at", None)
        if comparable_existing != comparable_payload:
            fail(f"immutable artifact manifest already exists with different content: {path}")
        return
    path.write_bytes(serialized)


def create_manifest(root: Path, apk: Path) -> dict[str, Any]:
    version, version_code, package = read_app_config(root)
    source_revision, fixed_origin_revision, qa_branch = build_source_state(root)
    if qa_branch is not None:
        reject_qa_release_marker(apk)
    gate = release_gate(root, os.environ.get("AOITALK_MOBILE_RELEASE_BASE", "").strip(), source_revision)
    if not gate["release_required"] or not gate["version_bump_valid"]:
        fail("canonical Mobile release gate did not require and pass a release")
    installer = verify_with_canonical_script(root, apk, version, version_code)
    if qa_branch is not None:
        installer["source_revision"] = source_revision
    signature = signature_evidence(apk)
    digest = sha256_file(apk)
    payload = {
        "schema": QA_BUILD_SCHEMA if qa_branch is not None else SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact": {
            "path": relative_path(root, apk),
            "bytes": apk.stat().st_size,
            "sha256": digest,
            "package": package,
            "version": version,
            "version_code": version_code,
        },
        "source": {
            "revision": source_revision,
            "fixed_origin_revision": fixed_origin_revision,
            "mobile_input_hashes": mobile_input_hashes(root),
        },
        "release_gate": gate,
        "verification": {"installer": installer, "signature": signature},
    }
    if qa_branch is not None:
        payload["build_mode"] = "qa"
        payload["publishable"] = False
        payload["source"]["branch"] = qa_branch
        payload["source"]["remote_revision"] = source_revision
        # Recheck the pinned clean remote source after APK/signature inspection.
        if build_source_state(root) != (source_revision, fixed_origin_revision, qa_branch):
            fail("QA source changed during APK verification")
        reject_qa_release_marker(apk)
    write_json_immutable(manifest_path_for(apk), payload)
    return payload


def require_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        fail(f"manifest field is missing: {label}.{key}")
    return value


def validate_manifest(root: Path, apk: Path) -> dict[str, Any]:
    if qa_build_requested():
        fail("QA branch-build mode cannot validate artifacts for publication")
    manifest_path = manifest_path_for(apk)
    assert_regular_sidecar(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"artifact manifest is missing or invalid: {manifest_path}: {exc}")
    if not isinstance(payload, dict):
        fail("artifact manifest must be a JSON object")
    if payload.get("schema") == QA_BUILD_SCHEMA or payload.get("build_mode") == "qa" or payload.get("publishable") is False:
        fail("QA branch-build artifacts cannot be approved or published")
    if strict_string(payload.get("schema"), "manifest schema") != SCHEMA:
        fail("artifact manifest schema is missing or unsupported")
    artifact = payload.get("artifact")
    source = payload.get("source")
    gate = payload.get("release_gate")
    verification = payload.get("verification")
    if not all(isinstance(value, dict) for value in (artifact, source, gate, verification)):
        fail("artifact manifest has incomplete sections")
    expected_rel = relative_path(root, apk)
    if strict_string(artifact.get("path"), "manifest artifact.path") != expected_rel:
        fail(f"artifact path mismatch: {artifact.get('path')} != {expected_rel}")
    version, version_code, package = read_app_config(root)
    if strict_string(artifact.get("version"), "manifest artifact.version") != version:
        fail("artifact manifest version/versionCode does not match mobile/app.json")
    if strict_int(artifact.get("version_code"), "manifest artifact.version_code") != version_code:
        fail("artifact manifest version/versionCode does not match mobile/app.json")
    if strict_string(artifact.get("package"), "manifest artifact.package", PACKAGE_RE) != package:
        fail("artifact manifest package does not match mobile/app.json")
    if strict_int(artifact.get("bytes"), "manifest artifact.bytes") != apk.stat().st_size:
        fail("artifact byte count mismatch")
    if assert_hex(strict_string(artifact.get("sha256"), "manifest artifact.sha256"), HEX64, "artifact SHA-256") != sha256_file(apk):
        fail("artifact SHA-256 mismatch")
    build_source_revision = assert_hex(strict_string(source.get("revision"), "manifest source.revision"), HEX40, "source revision")
    build_fixed_origin = assert_hex(strict_string(source.get("fixed_origin_revision"), "manifest source.fixed_origin_revision"), HEX40, "fixed origin revision")
    if build_source_revision != build_fixed_origin:
        fail("recorded source revision and recorded fixed origin/main revision differ")
    current_revision, current_origin = source_state(root)
    assert_recorded_source_is_usable(root, build_source_revision, current_origin)
    input_hashes = source.get("mobile_input_hashes")
    if not isinstance(input_hashes, dict) or any(
        type(key) is not str or not key or type(value) is not str or not HEX64.fullmatch(value)
        for key, value in input_hashes.items()
    ):
        fail("manifest source.mobile_input_hashes has invalid types or digests")
    if input_hashes != mobile_input_hashes(root):
        fail("current mobile build input hashes differ from the immutable artifact manifest")
    if strict_string(gate.get("base"), "manifest release_gate.base", HEX40) != gate.get("base") or \
        strict_string(gate.get("target"), "manifest release_gate.target", HEX40) != gate.get("target"):
        fail("artifact manifest release gate revisions are invalid")
    strict_bool(gate.get("mobile_changed"), "manifest release_gate.mobile_changed")
    if strict_bool(gate.get("release_required"), "manifest release_gate.release_required") is not True or \
        strict_bool(gate.get("version_bump_valid"), "manifest release_gate.version_bump_valid") is not True:
        fail("artifact manifest does not contain a passing required Mobile release gate")
    gate_base = require_string(gate, "base", "release_gate")
    if strict_string(gate.get("target"), "manifest release_gate.target", HEX40) != build_source_revision:
        fail("recorded Mobile release gate target differs from the recorded build source")
    fresh_build_gate = release_gate(root, gate_base, build_source_revision)
    if fresh_build_gate != gate:
        fail("recorded Mobile release gate differs from the immutable artifact manifest")
    current_gate = release_gate(root, gate_base, current_origin)
    if not current_gate["release_required"] or not current_gate["version_bump_valid"]:
        fail("current fixed origin/main no longer satisfies the Mobile release gate")
    if current_gate["base"] != gate_base:
        fail("current Mobile release gate base differs from the recorded gate")
    current_version, current_version_code, _ = read_app_config(root)
    if current_version != version or current_version_code != version_code:
        fail("current mobile/app.json version differs from the recorded APK")
    installer = verify_with_canonical_script(root, apk, version, version_code)
    signature = signature_evidence(apk)
    manifest_installer = verification.get("installer")
    manifest_signature = verification.get("signature")
    if not isinstance(manifest_installer, dict) or not isinstance(manifest_signature, dict):
        fail("artifact manifest verification sections are invalid")
    if strict_bool(manifest_installer.get("apk_installer_module"), "manifest installer module") is not True or \
        strict_bool(manifest_installer.get("request_install_packages"), "manifest installer permission") is not True or \
        strict_string(manifest_installer.get("version"), "manifest installer version") != version or \
        strict_int(manifest_installer.get("version_code"), "manifest installer version_code") != version_code:
        fail("artifact manifest installer evidence is invalid")
    if strict_string(manifest_signature.get("scheme"), "manifest signature scheme") != "v2" or \
        strict_int(manifest_signature.get("signer_count"), "manifest signer_count") != 1 or \
        assert_hex(strict_string(manifest_signature.get("certificate_sha256"), "manifest certificate_sha256"), HEX64, "manifest certificate_sha256") != signature["certificate_sha256"]:
        fail("artifact manifest signature evidence is invalid")
    if manifest_installer != installer or manifest_signature != signature:
        fail("APK installer/signature evidence differs from the immutable artifact manifest")
    qa_path = qa_marker_path_for(apk)
    assert_regular_sidecar(qa_path)
    try:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"separate QA approval marker is missing or invalid: {qa_path}: {exc}")
    if not isinstance(qa, dict):
        fail("QA approval marker must be a JSON object")
    if strict_string(qa.get("schema"), "QA approval schema") != QA_SCHEMA or \
        strict_bool(qa.get("approved"), "QA approval approved") is not True:
        fail("QA approval marker is missing, unsupported, or not approved")
    for key, expected in (
        ("artifact_sha256", artifact["sha256"]),
        ("version", version),
        ("version_code", version_code),
        ("source_revision", build_source_revision),
        ("certificate_sha256", signature["certificate_sha256"]),
    ):
        actual = qa.get(key)
        if key == "version":
            actual = strict_string(actual, "QA approval version")
        elif key == "version_code":
            actual = strict_int(actual, "QA approval version_code")
        elif key == "source_revision":
            actual = assert_hex(str(actual), HEX40, "QA approval source_revision")
        elif key in ("artifact_sha256", "certificate_sha256"):
            actual = assert_hex(str(actual), HEX64, f"QA approval {key}")
        if actual != expected:
            fail(f"QA approval marker mismatch: {key}")
    return {
        "manifest": str(manifest_path),
        "qa_marker": str(qa_path),
        "artifact_sha256": artifact["sha256"],
        "bytes": artifact["bytes"],
        "version": version,
        "version_code": version_code,
        "build_source_revision": build_source_revision,
        "current_validated_revision": current_revision,
        "certificate_sha256": signature["certificate_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "write", "validate", "invalidate"))
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--apk-path", required=True)
    args = parser.parse_args()
    try:
        root = Path(args.project_root).resolve(strict=True)
        qa_build = qa_build_requested()
        if qa_build and args.mode == "validate":
            fail("QA branch-build mode cannot validate artifacts for publication")
        apk = project_path(root, args.apk_path, strict=args.mode not in ("invalidate", "preflight"))
        if args.mode not in ("invalidate", "preflight") and not apk.is_file():
            fail(f"APK is missing: {apk}")
        if args.mode == "invalidate":
            if qa_build:
                reject_qa_release_marker(apk)
            invalidate_sidecars(apk)
            print("MOBILE_ARTIFACT_SIDECARS_INVALIDATED=True")
            return 0
        if args.mode == "preflight":
            version, version_code, package = read_app_config(root)
            source_revision, fixed_origin_revision, qa_branch = build_source_state(root)
            if qa_build:
                reject_qa_release_marker(apk)
            gate = release_gate(root, os.environ.get("AOITALK_MOBILE_RELEASE_BASE", "").strip(), source_revision)
            if not gate["release_required"] or not gate["version_bump_valid"]:
                fail("canonical Mobile release gate did not require and pass a release")
            print(json.dumps({"version": version, "version_code": version_code, "package": package, "source_revision": source_revision, "fixed_origin_revision": fixed_origin_revision, "qa_branch": qa_branch, "publishable": not qa_build, "release_gate": gate}, ensure_ascii=False, sort_keys=True))
            print("MOBILE_ARTIFACT_PREFLIGHT=True")
            return 0
        payload = create_manifest(root, apk) if args.mode == "write" else validate_manifest(root, apk)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if args.mode == "validate":
            print("MOBILE_ARTIFACT_VALIDATED=True")
        else:
            print("MOBILE_ARTIFACT_MANIFEST_WRITTEN=True")
        return 0
    except ManifestError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
