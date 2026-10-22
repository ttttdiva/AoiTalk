#!/usr/bin/env python3
"""Official connected-PC producer for source-bound Enterprise build inputs.

collect: materialize OfflineInputRoot from a sanitized handoff manifest. The
canonical PowerShell packager calls this before packaging its companion.
No operator-created cache/wheel/APT layout is required or consumed.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True  # Never mutate the checksum-bound source/release.

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile

from enterprise_release_common import (
    COMMIT, ContractError, atomic_json, binding_digest, file_records, hash_file,
    no_links, preflight_publication, publish_directory, read_json, regular_files, tree_digest,
)
from offline_registry_source import RegistrySource

PROOF = "provenance/closure-check.json"
EXCLUDED = {"offline-build-manifest.json", "offline-input-manifest.json", PROOF}


def input_digest(root: Path) -> str:
    rows = file_records(root, EXCLUDED)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dependency_hashes(source: Path) -> dict[str, str]:
    paths = ("Dockerfile", "pyproject.toml", "frontend/package.json", "frontend/package-lock.json",
             "docker/install-enterprise-system-deps.sh")
    return {rel: hash_file(source / rel) for rel in paths}


def inspect_source(handoff: Path) -> tuple[dict, Path]:
    no_links(handoff)
    manifest = read_json(handoff)
    source = handoff.parent / "source"
    if not re.fullmatch(r"[a-z][a-z0-9]*-enterprise-handoff", str(manifest.get("format", ""))):
        raise ContractError("unsupported handoff format")
    if not COMMIT.fullmatch(str(manifest.get("source_commit", ""))) or manifest.get("source_dirty") is not False:
        raise ContractError("handoff must name a clean fixed source commit")
    contract = manifest.get("source_tree", {})
    if contract.get("commit") != manifest["source_commit"] or contract.get("sanitized") is not True:
        raise ContractError("producer requires the canonical sanitized source tree")
    if manifest.get("target_build", {}).get("architecture") != "linux/amd64":
        raise ContractError("unsupported target build architecture")
    dockerfile = (source / "Dockerfile").read_text(encoding="utf-8-sig")
    if re.search(r"(?im)^[ \t]*#[ \t]*syntax[ \t]*=", dockerfile):
        raise ContractError("offline handoff Dockerfile must use the bundled frontend without a syntax directive")
    files = regular_files(source)
    if not files or any(p.name == ".env" or p.suffix.lower() in {".gguf", ".safetensors", ".key", ".pem", ".pfx"} for p in files):
        raise ContractError("source violates the producer secret/model boundary")
    return manifest, source


class DockerProducer:
    def __init__(self, executable: str = "docker"):
        resolved = shutil.which(executable)
        if not resolved:
            raise ContractError("Docker CLI is required on the connected producer PC")
        self.executable = resolved

    def run(self, arguments: list[str]) -> None:
        # No host package-manager configuration, proxy secrets, registry login
        # files, model directories, or host caches are mounted in a container.
        result = subprocess.run([self.executable, *arguments], check=False)
        if result.returncode:
            raise ContractError(f"producer Docker operation failed (exit={result.returncode})")

    @staticmethod
    def mount(path: Path, destination: str, *, readonly=False) -> list[str]:
        path = no_links(path)
        if "," in str(path) or any(c in str(path) for c in "\r\n"):
            raise ContractError("Docker bind path contains an option separator")
        value = f"type=bind,source={path},target={destination}"
        return ["--mount", value + (",readonly" if readonly else "")]

    def worker(self, ref: str, source: Path, handoff: Path, output: Path, phase: str) -> None:
        name = "enterprise-collect-" + secrets.token_hex(8)
        args = ["run", "--name", name, "--rm", "--platform", "linux/amd64", "--pull=never",
                *self.mount(source, "/source", readonly=True), *self.mount(handoff, "/handoff.json", readonly=True),
                *self.mount(output, "/output")]
        if phase == "npm":
            args += [ref, "node", "/source/deploy/enterprise/collect_offline_npm.mjs", "/source", "/output"]
        else:
            args += [ref, "python", "/source/deploy/enterprise/collect_offline_deps.py", phase,
                     "--source", "/source", "--root", "/output", "--handoff", "/handoff.json"]
        try:
            self.run(args)
        finally:
            # Also clean up on SIGINT/subprocess failure; the unique name cannot
            # target a user's existing container.
            subprocess.run([self.executable, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    @staticmethod
    def build_arguments(source: Path, output: Path, pins: list[dict], secret: Path, tag: str, iid: Path) -> list[str]:
        dockerfile = (source / "Dockerfile").read_text(encoding="utf-8-sig")
        if re.search(r"(?im)^[ \t]*#[ \t]*syntax[ \t]*=", dockerfile):
            raise ContractError("offline handoff Dockerfile must use the bundled frontend without a syntax directive")
        prefixes = set(re.findall(r"(?m)^ARG ([A-Z][A-Z0-9_]*)_OFFLINE_BUILD=0\s*$", dockerfile))
        if len(prefixes) != 1:
            raise ContractError("Dockerfile offline build argument is missing or ambiguous")
        prefix = prefixes.pop()
        args = ["buildx", "build", "--platform", "linux/amd64", "--pull=false", "--no-cache", "--network=none",
                "--build-arg", f"{prefix}_OFFLINE_BUILD=1",
                "--build-arg", "NPM_INSTALL_MODE=offline", "--build-context", f"offline-inputs={output}",
                "--build-context", f"npm-cache={output / 'npm-cache'}", "--secret", f"id=nextauth_secret,src={secret}",
                "--load", "--tag", tag, "--iidfile", str(iid), "--progress=plain"]
        selected = {row["name"]: row for row in pins}
        for name, context in (("dockerfile-node", "enterprise-node-base"), ("dockerfile-python", "enterprise-python-base")):
            row = selected[name]
            # as_uri gives a canonical Windows/Unix path; replace only the
            # scheme, preserving file:///C:/... and file:///tmp/... semantics.
            layout_uri = (output / row["oci_layout"]).resolve().as_uri().replace("file:", "oci-layout:", 1)
            args += ["--build-context", f"{context}={layout_uri}@{row['platform_manifest_digest']}"]
        return [*args, str(source)]

    def prove(self, source: Path, output: Path, pins: list[dict]) -> None:
        tag = "enterprise-offline-proof:" + secrets.token_hex(12)
        with tempfile.TemporaryDirectory(prefix="enterprise-proof-") as raw:
            scratch = Path(raw)
            secret, iid = scratch / "build-secret", scratch / "image-id"
            secret.write_text(secrets.token_hex(32), encoding="ascii")
            os.chmod(secret, 0o600)
            try:
                self.run(self.build_arguments(source, output, pins, secret, tag, iid))
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", iid.read_text().strip()):
                    raise ContractError("offline closure build did not produce a local image ID")
            finally:
                subprocess.run([self.executable, "image", "rm", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def verify_proof(root: Path, handoff: dict, source: Path) -> None:
    proof = read_json(root / PROOF)
    expected = {"format": "enterprise-offline-closure-proof", "version": 1, "result": "pass",
                "platform": "linux/amd64", "network": "none", "dockerfile_frontend": "dockerfile.v0",
                "source_commit": handoff["source_commit"], "source_tree_sha256": tree_digest(source),
                "handoff_binding_sha256": binding_digest(handoff), "input_files_sha256": input_digest(root),
                "dependency_definitions": dependency_hashes(source)}
    if proof != expected:
        raise ContractError("producer closure proof is missing, failed, or bound to different inputs")



def verify_completed_inputs(root: Path, handoff: Path) -> dict:
    """Revalidate every byte and the proof against this exact source handoff."""
    from offline_inputs import verify_companion

    manifest, source = inspect_source(handoff)
    if (manifest.get("offline_producer") or {}).get("version") != 1:
        raise ContractError("handoff does not declare the supported producer contract")
    root = no_links(root)
    verify_proof(root, manifest, source)
    document = read_json(root / "offline-build-manifest.json")
    # A private synthetic embedded binding invokes the unchanged canonical
    # consumer without editing the caller's handoff, source or checkpoint.
    with tempfile.TemporaryDirectory(prefix="enterprise-input-verify-") as raw:
        verify_root = Path(raw)
        shutil.copytree(source, verify_root / "source")
        check = dict(manifest)
        check["offline_build"] = dict(manifest.get("offline_build", {}),
            format=document["format"], version=1, enabled=True, mode="embedded", source_commit=manifest["source_commit"],
            source_tree_sha256=tree_digest(source), handoff_binding_sha256=binding_digest(manifest),
            manifest_sha256=hash_file(root / "offline-build-manifest.json"),
            npm_lock_sha256=hash_file(source / "frontend/package-lock.json"), pyproject_sha256=hash_file(source / "pyproject.toml"))
        atomic_json(verify_root / "bundle-manifest.json", check)
        verify_companion(root, verify_root / "bundle-manifest.json")
    return document


def retain_completed_oci(staging: Path, pins: list[dict], parent: Path) -> Path | None:
    """Keep only completed public OCI layouts as untrusted retry-cache input."""
    if not pins:
        return None
    parent = no_links(parent)
    if not parent.is_dir():
        raise ContractError("OCI retry cache parent must be an existing directory")
    layouts = []
    seen = set()
    for pin in pins:
        name = pin.get("name", "")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
                or name in seen or pin.get("oci_layout") != f"images/{name}"):
            raise ContractError("invalid completed OCI layout")
        seen.add(name)
        layout = no_links(staging / "images" / name)
        for child in layout.rglob("*"):
            no_links(child)
            if child.is_dir() and child.relative_to(layout).as_posix() not in {"blobs", "blobs/sha256"}:
                raise ContractError("unexpected directory in completed OCI layout")
        files = regular_files(layout)
        names = {p.relative_to(layout).as_posix() for p in files}
        if not {"index.json", "oci-layout"} <= names or any(
                name not in {"index.json", "oci-layout"}
                and not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name) for name in names):
            raise ContractError("unexpected file in completed OCI layout")
        layouts.append((name, layout))
    cache = Path(tempfile.mkdtemp(prefix=".enterprise-oci-retry-", dir=parent))
    try:
        images = cache / "images"
        images.mkdir(mode=0o700)
        for name, layout in layouts:
            # Same-filesystem rename avoids another multi-GiB copy on failure.
            # The existing cache consumer still re-fetches descriptors and hashes
            # every copied blob; retaining bytes grants no release authority.
            os.rename(layout, images / name)
        return cache
    except BaseException:
        shutil.rmtree(cache, ignore_errors=True)
        raise


def collect(handoff: Path, destination: Path, *, docker=None, registry=None, blob_cache: Path | None = None,
            failure_cache_parent: Path | None = None) -> dict:
    manifest, source = inspect_source(handoff)
    if (manifest.get("offline_producer") or {}).get("version") != 1:
        raise ContractError("handoff does not declare the supported producer contract")
    source_hash = tree_digest(source)
    destination = no_links(destination)
    if destination.exists() or source == destination or source in destination.parents or destination in source.parents:
        raise ContractError("producer output must be a new directory outside source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    preflight_publication(destination.parent)
    failure_cache_parent = no_links(failure_cache_parent or destination.parent)
    if failure_cache_parent == source or source in failure_cache_parent.parents:
        raise ContractError("OCI retry cache must remain outside source")
    docker = docker or DockerProducer()
    if blob_cache is not None:
        blob_cache = no_links(blob_cache)
        if any(blob_cache == root or blob_cache in root.parents or root in blob_cache.parents for root in (source, destination)):
            raise ContractError("OCI blob cache must not overlap source or output")
        if registry is not None:
            raise ContractError("OCI blob cache cannot be combined with a registry override")
    registry = registry or RegistrySource(blob_cache=blob_cache)
    pins = [row for row in manifest.get("image_pins", []) if row.get("kind") in {"dependency", "build-base"}]
    if not pins or len({row["name"] for row in pins}) != len(pins):
        raise ContractError("invalid handoff image pin set")
    bases = {row["name"]: row["ref"] for row in pins if row["kind"] == "build-base"}
    if set(bases) != {"dockerfile-node", "dockerfile-python"}:
        raise ContractError("producer requires the exact Node/Python base set")
    staging = Path(tempfile.mkdtemp(prefix=".enterprise-collect-", dir=destination.parent))
    image_pins = []
    completed = False
    try:
        for row in pins:
            print(f"[Enterprise producer] OCI: {row['name']}", flush=True)
            image_pins.append(registry.collect(row, staging))
        for ref in bases.values():
            docker.run(["pull", "--platform", "linux/amd64", ref])
        for phase in ("apt", "python", "npm"):
            ref = bases["dockerfile-node" if phase == "npm" else "dockerfile-python"]
            print(f"[Enterprise producer] {phase} closure", flush=True)
            docker.worker(ref, source, handoff, staging, phase)
        document = {"format": manifest["format"].replace("-enterprise-handoff", "-enterprise-offline-inputs"),
                    "version": 1, "platform": "linux/amd64", "source_commit": manifest["source_commit"],
                    "source_tree_sha256": source_hash, "handoff_binding_sha256": binding_digest(manifest), "image_pins": image_pins}
        for phase in ("apt", "python", "npm"):
            result_path = staging / f"{phase}-collection.json"
            document.update(read_json(result_path))
            result_path.unlink()
        document["files"] = file_records(staging, {"offline-build-manifest.json"})
        atomic_json(staging / "offline-build-manifest.json", document)
        # Fail on opaque/incomplete OCI, .deb control/index metadata and wheels
        # before spending time in the real no-network production build.
        from offline_inputs import validate_payload
        validate_payload(staging)
        docker.prove(source, staging, image_pins)
        if tree_digest(source) != source_hash:
            raise ContractError("source changed during dependency acquisition")
        proof = {"format": "enterprise-offline-closure-proof", "version": 1, "result": "pass", "platform": "linux/amd64",
                 "network": "none", "dockerfile_frontend": "dockerfile.v0", "source_commit": manifest["source_commit"],
                 "source_tree_sha256": source_hash, "handoff_binding_sha256": binding_digest(manifest),
                 "input_files_sha256": input_digest(staging), "dependency_definitions": dependency_hashes(source)}
        atomic_json(staging / PROOF, proof)
        document["files"] = file_records(staging, {"offline-build-manifest.json"})
        if sum(row["size_bytes"] for row in document["files"]) > 64 * 1024**3:
            raise ContractError("complete companion exceeds the 64GiB safety limit")
        atomic_json(staging / "offline-build-manifest.json", document)
        verify_completed_inputs(staging, handoff)
        completed = True
        # The release packager serializes its output directory. Refuse reuse;
        # never merge a failed attempt with a previous companion.
        if destination.exists():
            raise ContractError("producer output appeared during collection")
        publish_directory(staging, destination)
        return {"status": "complete", "source_commit": manifest["source_commit"], "manifest_sha256": hash_file(destination / "offline-build-manifest.json")}
    except BaseException:
        if completed:
            # Publication may fail on the filesystem after the expensive proof.
            # Keep the exact validated tree in place; retaining it must not rely
            # on another rename, and must not split out its OCI layouts.
            checkpoint = staging if staging.exists() else destination
            print(f"[Enterprise producer] Validated input checkpoint retained: {checkpoint}",
                  file=sys.stderr, flush=True)
        else:
            try:
                cache = retain_completed_oci(staging, image_pins, failure_cache_parent)
                if cache is not None:
                    print(f"[Enterprise producer] OCI retry cache retained: {cache}", file=sys.stderr, flush=True)
            except Exception as retention_error:
                print("[Enterprise producer] OCI retry cache retention failed: "
                      + type(retention_error).__name__, file=sys.stderr, flush=True)
        raise
    finally:
        if not completed and staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    producer = sub.add_parser("collect")
    producer.add_argument("--handoff", required=True, type=Path)
    producer.add_argument("--output", required=True, type=Path)
    producer.add_argument("--oci-blob-cache", type=Path,
                          help="optional read-only previous producer directory; only SHA256/size-verified OCI config/layer bytes are reused")
    args = parser.parse_args()
    try:
        if args.oci_blob_cache is None:
            result = collect(args.handoff, args.output)
        else:
            result = collect(args.handoff, args.output, blob_cache=args.oci_blob_cache)
    except ContractError as exc:
        # ContractError messages are deliberately authored by this producer and
        # contain no command output, bearer token, or credential-bearing URL.
        # Surface the reason so a missing local prerequisite does not collapse
        # into an opaque "ContractError" while keeping subprocess/OS failures
        # redacted below.
        parser.exit(1, f"Enterprise producer failed: ContractError: {exc}\n")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        # Do not echo arbitrary OS/subprocess details or a URL that could contain
        # a transient registry credential. Detailed dependency failures remain
        # in the producer/Docker logs.
        parser.exit(1, f"Enterprise producer failed: {type(exc).__name__}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
