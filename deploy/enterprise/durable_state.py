#!/usr/bin/env python3
"""Enterprise PostgreSQL/Qdrant checkpoint and recovery interface (version 1).

Checkpoints are private target-local state, never handoff contents. Services
are stopped together before physical backups. Restoration uses the previous
release's exact local images and rendered Compose configuration, never builds
an old release and never attempts to downgrade a candidate database in place.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True  # Never mutate the checksum-bound source/release.

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
import socket
import time
from urllib.parse import quote
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import uuid

from enterprise_release_common import (
    BLOCK, COMMIT, ContractError, atomic_json, fsync_directory, hash_file,
    no_links, read_json, regular_files, relative_path, verify_checksums,
)

FORMAT = "aoitalk-enterprise-durable-state"
COMPONENTS = ("postgres", "qdrant", "release-state")
TERMINAL = {"COMMITTED", "RESTORED", "ABORTED"}
RECOVERY_MODULES = ("durable_state.py", "enterprise_release_common.py")


def secure_directory(path: Path, *, create=False) -> Path:
    path = no_links(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ContractError("state directory is missing")
    for parent in (path, *path.parents):
        info = parent.stat()
        if info.st_uid != 0:
            raise ContractError("state path must be root-owned")
        if info.st_mode & 0o022 and not (parent == Path("/tmp") and info.st_mode & stat.S_ISVTX):
            raise ContractError("state path must not be group/world writable")
    return path


def private_file(path: Path) -> None:
    no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ContractError("checkpoint metadata must be a private root-owned regular file")


def archive_tree(root: Path, component: str, archive: Path) -> None:
    directory = root / component
    # Reject links/tablespaces and special files rather than silently omitting
    # externally stored durable data. The supported contract is managed bind
    # storage with a single-node Qdrant service.
    regular_files(directory)
    with tarfile.open(archive, "x", format=tarfile.PAX_FORMAT, dereference=True) as tar:
        for path in [directory, *sorted(directory.rglob("*"))]:
            tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    os.chmod(archive, 0o600)
    with archive.open("rb") as stream:
        os.fsync(stream.fileno())


def archive_matches(archive: Path, root: Path, component: str) -> bool:
    expected = {}
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            name = relative_path(member.name)
            if name.split("/")[0] != component or not (member.isdir() or member.isreg()):
                raise ContractError("invalid recovery archive member")
            if member.isreg():
                stream = tar.extractfile(member)
                if stream is None:
                    return False
                h = hashlib.sha256()
                with stream:
                    for block in iter(lambda: stream.read(BLOCK), b""):
                        h.update(block)
                expected[name] = (member.size, h.hexdigest(), member.uid, member.gid, member.mode & 0o7777)
    actual = {}
    for path in regular_files(root / component):
        info = path.stat()
        actual[path.relative_to(root).as_posix()] = (info.st_size, hash_file(path), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    return expected == actual


def extract_tree(archive: Path, destination: Path, component: str) -> None:
    with tarfile.open(archive, "r:") as tar:
        members, seen = tar.getmembers(), set()
        for member in members:
            name = relative_path(member.name)
            if name.split("/")[0] != component or name.casefold() in seen:
                raise ContractError("checkpoint archive path/coverage mismatch")
            seen.add(name.casefold())
            if not member.isdir() and not member.isreg():
                raise ContractError("checkpoint contains a link or special file")
        if component not in seen:
            raise ContractError("checkpoint archive lacks its component root")
        # Manual extraction is independent of Python tarfile filter defaults.
        for member in members:
            path = destination / member.name
            no_links(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            else:
                source = tar.extractfile(member)
                if source is None:
                    raise ContractError("checkpoint member cannot be read")
                with source, path.open("xb") as output:
                    shutil.copyfileobj(source, output, BLOCK)
                    output.flush()
                    os.fsync(output.fileno())
                if path.stat().st_size != member.size:
                    raise ContractError("checkpoint member size mismatch")
        # Restore directory timestamps/modes after their children have been
        # materialized. Numeric UIDs/GIDs are essential for PostgreSQL.
        for member in reversed(members):
            path = destination / member.name
            os.chown(path, member.uid, member.gid)
            os.chmod(path, member.mode & 0o7777)
            os.utime(path, (member.mtime, member.mtime))
            # Persist both file content and the final ownership/mode, then all
            # directory entries, before any durable-store rename can occur.
            flags = os.O_RDONLY | (os.O_DIRECTORY if member.isdir() else 0)
            fd = os.open(path, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_directory(destination)


class DockerRuntime:
    def __init__(self, project: str):
        self.project = project

    def command(self, args: list[str]) -> str:
        result = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *args], text=True, capture_output=True, check=False)
        if result.returncode:
            # Rendered config and subprocess diagnostics may contain private
            # target configuration. Never return those to a journal or stdout.
            raise ContractError("durable-state Docker operation failed")
        return result.stdout

    def containers(self, *, all_projects=False) -> list[dict]:
        args = ["ps", "-aq"]
        if not all_projects:
            args += ["--filter", f"label=com.docker.compose.project={self.project}"]
        ids = self.command(args).split()
        if not ids:
            return []
        rows = json.loads(self.command(["inspect", *ids]))
        if not all_projects and any(row.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") != self.project for row in rows):
            raise ContractError("foreign container returned for managed project")
        return rows

    def capture_runtime(self, previous: Path, install: Path, data: Path) -> dict:
        rows = self.containers()
        running = {row["Config"]["Labels"].get("com.docker.compose.service"): row for row in rows if row["State"]["Running"]}
        if not {"postgres", "qdrant"}.issubset(running):
            raise ContractError("managed PostgreSQL and Qdrant must be running before checkpoint")
        for name, destination in (("postgres", "/var/lib/postgresql/data"), ("qdrant", "/qdrant/storage")):
            mounts = [m for m in running[name].get("Mounts", []) if m.get("Destination") == destination]
            if len(mounts) != 1 or mounts[0].get("Type") != "bind" or mounts[0].get("Source") != str(data / name):
                raise ContractError("durable service uses an unsupported storage topology")
        services, containers = {}, []
        for row in rows:
            labels = row["Config"].get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.oneoff", "False").lower() == "true":
                continue
            if not service or service in services:
                raise ContractError("checkpoint supports one container per managed service")
            config = dict(row["Config"])
            config["Image"] = row["Image"]
            services[service] = {"image": row["Image"]}
            networks = {}
            for network, endpoint in row.get("NetworkSettings", {}).get("Networks", {}).items():
                networks[network] = {k: endpoint[k] for k in ("IPAMConfig", "Aliases", "Links", "DriverOpts") if endpoint.get(k)}
            containers.append({"name": row["Name"].lstrip("/"), "service": service, "running": row["State"]["Running"],
                               "Config": config, "HostConfig": row["HostConfig"], "NetworkingConfig": {"EndpointsConfig": networks}})
        result = {"format": "enterprise-frozen-container-runtime", "version": 1, "name": self.project,
                  "services": services, "containers": containers}
        # Resolve any bind source that used current/ before the pointer moves.
        return json.loads(json.dumps(result).replace(str(install / "current") + "/", str(previous) + "/"))

    def pin_config(self, config: dict, previous: Path, install: Path, data: Path) -> dict:
        if not config:
            return self.capture_runtime(previous, install, data)
        services = config.get("services", {})
        if not {"postgres", "qdrant"}.issubset(services):
            raise ContractError("rendered Compose configuration lacks durable services")
        rows = self.containers()
        running = {row["Config"]["Labels"].get("com.docker.compose.service"): row for row in rows if row["State"]["Running"]}
        for name, destination in (("postgres", "/var/lib/postgresql/data"), ("qdrant", "/qdrant/storage")):
            row = running.get(name)
            if row is None:
                raise ContractError("managed durable service must be running before checkpoint")
            matching = [m for m in row.get("Mounts", []) if m.get("Destination") == destination]
            if len(matching) != 1 or matching[0].get("Type") != "bind" or matching[0].get("Source") != str(data / name):
                raise ContractError("durable service uses an unsupported storage topology")
        result = json.loads(json.dumps(config))
        result["name"] = self.project
        for name, service in result["services"].items():
            reference = service.get("image")
            if not reference:
                raise ContractError("every restore service requires an existing image")
            image = json.loads(self.command(["image", "inspect", reference]))[0]
            image_id = image.get("Id", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ContractError("restore image lacks an immutable local ID")
            if name in running and running[name].get("Image") != image_id:
                raise ContractError("rendered Compose image differs from the running release")
            service["image"] = image_id
            service["pull_policy"] = "never"
            service.pop("build", None)
        # Bind mounts must refer to the immutable previous release, not to a
        # current symlink that will move to the candidate.
        text = json.dumps(result).replace(str(install / "current") + "/", str(previous) + "/")
        return json.loads(text)

    def assert_no_foreign_writers(self, data: Path) -> None:
        roots = [data / name for name in ("postgres", "qdrant")]
        for row in self.containers(all_projects=True):
            if not row.get("State", {}).get("Running"):
                continue
            own = row.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") == self.project
            for mount in row.get("Mounts", []):
                if not mount.get("RW") or mount.get("Type") != "bind":
                    continue
                mounted = Path(mount.get("Source", ""))
                if any(mounted == root or mounted in root.parents or root in mounted.parents for root in roots) and not own:
                    raise ContractError("another container can write managed durable storage")

    def stop(self, data: Path, *, require_clean: bool = True) -> None:
        self.assert_no_foreign_writers(data)
        rows = self.containers()
        ids = [row["Id"] for row in rows]
        if ids:
            self.command(["update", "--restart=no", *ids])
            self.command(["stop", "--time", "120", *ids])
        if any(row["State"]["Running"] for row in self.containers()):
            raise ContractError("a managed service is still running")
        self.assert_no_foreign_writers(data)
        if require_clean and (data / "postgres/postmaster.pid").exists():
            raise ContractError("PostgreSQL did not shut down cleanly")

    def verify_images(self, config: dict) -> None:
        for service in config["services"].values():
            ref = service["image"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
                raise ContractError("restore config contains a mutable image")
            rows = json.loads(self.command(["image", "inspect", ref]))
            if len(rows) != 1 or rows[0].get("Id") != ref:
                raise ContractError("previous release image is no longer locally available")

    @staticmethod
    def engine(method: str, path: str, payload: dict | None = None) -> dict:
        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(120)
                self.sock.connect("/var/run/docker.sock")
        connection = UnixConnection("localhost", timeout=120)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
            if response.status not in {200, 201, 204, 304}:
                raise ContractError(f"local Docker restore operation failed (HTTP {response.status})")
            return json.loads(raw) if raw else {}
        finally:
            connection.close()

    def start(self, config_file: Path) -> None:
        frozen = read_json(config_file)
        if frozen.get("format") != "enterprise-frozen-container-runtime":
            self.command(["compose", "--project-name", self.project, "--file", str(config_file),
                          "up", "-d", "--remove-orphans", "--no-build", "--pull", "never", "--wait"])
            return
        self.verify_images(frozen)
        current = self.containers()
        if any(row["State"]["Running"] for row in current):
            raise ContractError("restore requires all candidate containers stopped")
        if current:
            self.command(["rm", *[row["Id"] for row in current]])
        desired = []
        for row in frozen["containers"]:
            # A successfully completed storage-init does not need recreation.
            # It would otherwise lose its successful exit state on Docker create.
            if not row["running"]:
                continue
            config = dict(row["Config"])
            config["HostConfig"] = row["HostConfig"]
            config["NetworkingConfig"] = row["NetworkingConfig"]
            result = self.engine("POST", "/containers/create?name=" + quote(row["name"], safe=""), config)
            desired.append((row["service"], result["Id"], config["Image"]))
        desired.sort(key=lambda row: (row[0] not in {"postgres", "qdrant"}, row[0]))
        for _, container_id, _ in desired:
            self.command(["start", container_id])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            rows = json.loads(self.command(["inspect", *[row[1] for row in desired]]))
            by_id = {row["Id"]: row for row in rows}
            healthy = True
            for _, container_id, image_id in desired:
                row = by_id[container_id]
                if row.get("Image") != image_id:
                    raise ContractError("restored runtime image identity mismatch")
                state = row["State"]
                if not state.get("Running") or state.get("Health", {}).get("Status", "healthy") != "healthy":
                    healthy = False
            if healthy:
                return
            time.sleep(2)
        raise ContractError("restored runtime did not become healthy")

    def enable_restart(self) -> None:
        ids = [row["Id"] for row in self.containers() if row["State"]["Running"] and row["Config"].get("Labels", {}).get("com.docker.compose.oneoff", "False").lower() != "true"]
        if ids:
            self.command(["update", "--restart=unless-stopped", *ids])


class Checkpoints:
    def __init__(self, install: Path, data: Path, runtime):
        self.install = secure_directory(install)
        self.data = secure_directory(data)
        self.root = secure_directory(install / ".durable-state", create=True)
        self.active_path = self.root / "active.json"
        self.runtime = runtime

    def transaction(self, transaction_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            raise ContractError("invalid durable transaction ID")
        path = self.root / transaction_id
        secure_directory(path)
        private_file(path / "checkpoint.json")
        document = read_json(path / "checkpoint.json")
        if document.get("format") != FORMAT or document.get("version") != 1 or document.get("transaction") != transaction_id:
            raise ContractError("unsupported checkpoint contract")
        if document.get("install_root") != str(self.install) or document.get("data_root") != str(self.data):
            raise ContractError("checkpoint belongs to a different installation")
        recovery = document.get("recovery_modules")
        if not isinstance(recovery, dict) or set(recovery) != set(RECOVERY_MODULES):
            raise ContractError("checkpoint recovery implementation is incomplete")
        expected_entrypoint = str(path / "recovery" / "durable_state.py")
        if document.get("recovery_entrypoint") != expected_entrypoint:
            raise ContractError("checkpoint recovery entrypoint is invalid")
        for name, expected in recovery.items():
            module = path / "recovery" / name
            private_file(module)
            if hash_file(module) != expected:
                raise ContractError("checkpoint recovery implementation checksum mismatch")
        return path, document

    def active(self) -> tuple[Path, dict] | None:
        no_links(self.active_path)
        if not self.active_path.exists():
            return None
        private_file(self.active_path)
        return self.transaction(read_json(self.active_path)["transaction"])

    def current_release(self) -> Path:
        current = self.install / "current"
        if not current.is_symlink():
            raise ContractError("managed current pointer is missing")
        target = current.resolve(strict=True)
        if target.parent != self.install / "releases":
            raise ContractError("current pointer escapes managed releases")
        secure_directory(target)
        for entry in (target, *target.rglob("*")):
            info = entry.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ContractError("managed release entries must be root-owned and not group/world writable")
        verify_checksums(target)
        if not COMMIT.fullmatch(read_json(target / "bundle-manifest.json").get("source_commit", "")):
            raise ContractError("previous release lacks a canonical source identity")
        return target

    def save(self, path: Path, document: dict, phase: str | None = None) -> None:
        if phase:
            document["phase"] = phase
        atomic_json(path / "checkpoint.json", document)

    def begin(self, candidate_commit: str, config: dict) -> dict:
        if self.active():
            raise ContractError("RECOVERY_REQUIRED: an unfinished durable transaction exists")
        if not COMMIT.fullmatch(candidate_commit):
            raise ContractError("invalid candidate source commit")
        previous = self.current_release()
        prior_commit = read_json(previous / "bundle-manifest.json")["source_commit"]
        if prior_commit == candidate_commit:
            raise ContractError("same-commit update must not create a rollback checkpoint")
        for name in ("postgres", "qdrant"):
            regular_files(self.data / name)
        if not (self.data / "postgres/PG_VERSION").is_file():
            raise ContractError("managed PostgreSQL cluster is missing")
        config = self.runtime.pin_config(config, previous, self.install, self.data)
        transaction_id = uuid.uuid4().hex
        path = secure_directory(self.root / transaction_id, create=True)
        # The first update may be launched from a temporary incoming handoff,
        # while the previous release has no recovery interface. Persist the
        # exact trusted implementation before stopping services so recovery
        # remains executable after that staging tree has been removed.
        recovery = secure_directory(path / "recovery", create=True)
        recovery_modules = {}
        for name in RECOVERY_MODULES:
            source = no_links(Path(__file__).absolute().parent / name)
            target = recovery / name
            with source.open("rb") as input_stream, target.open("xb") as output:
                shutil.copyfileobj(input_stream, output, BLOCK)
                os.fchmod(output.fileno(), 0o600)
                output.flush()
                os.fsync(output.fileno())
            recovery_modules[name] = hash_file(target)
        fsync_directory(recovery)
        fsync_directory(path)
        atomic_json(path / "compose.json", config)
        metadata = {}
        for name in (".offline-inputs-required", "active-release"):
            source = self.install / name
            no_links(source)
            if source.exists() and not source.is_file():
                raise ContractError("release marker is not a regular file")
            metadata[name] = base64.b64encode(source.read_bytes()).decode() if source.exists() else None
        document = {"format": FORMAT, "version": 1, "transaction": transaction_id, "phase": "PREPARING",
                    "install_root": str(self.install), "data_root": str(self.data),
                    "previous_release": str(previous), "previous_commit": prior_commit, "candidate_commit": candidate_commit,
                    "previous_manifest_sha256": hash_file(previous / "bundle-manifest.json"),
                    "previous_checksums_sha256": hash_file(previous / "SHA256SUMS"),
                    "compose_sha256": hash_file(path / "compose.json"),
                    "recovery_entrypoint": str(recovery / "durable_state.py"), "recovery_modules": recovery_modules,
                    "release_markers": metadata, "artifacts": {}, "restored": []}
        self.save(path, document)
        atomic_json(self.active_path, {"transaction": transaction_id})
        # From this point a crash leaves a persistent recovery marker. Do not
        # clear it on exceptions or allow a candidate to mutate unbacked data.
        self.runtime.stop(self.data)
        required_space = sum(p.stat().st_size for name in COMPONENTS if (self.data / name).is_dir() for p in regular_files(self.data / name))
        if shutil.disk_usage(self.root).free < required_space + 64 * 1024**2:
            raise ContractError("insufficient checkpoint space; old services remain stopped for recover")
        for name in COMPONENTS:
            if not (self.data / name).exists():
                document["artifacts"][name] = None
                continue
            archive = path / (name + ".tar")
            archive_tree(self.data, name, archive)
            document["artifacts"][name] = {"file": archive.name, "size_bytes": archive.stat().st_size, "sha256": hash_file(archive)}
        self.save(path, document, "READY")
        return self.report(document)

    def previous_release(self, document: dict) -> Path:
        previous = Path(document["previous_release"])
        # Canonical adoption may rename the one approved historical directory.
        candidates = [previous, self.install / "releases" / document["previous_commit"]]
        for path in candidates:
            if path.parent == self.install / "releases" and path.is_dir() and not path.is_symlink():
                verify_checksums(path)
                if (hash_file(path / "bundle-manifest.json") == document["previous_manifest_sha256"]
                        and hash_file(path / "SHA256SUMS") == document["previous_checksums_sha256"]):
                    return path
        raise ContractError("previous release no longer matches checkpoint")

    def restore(self, transaction_id: str) -> dict:
        path, document = self.transaction(transaction_id)
        if document["phase"] in {"RESTORED", "ABORTED"}:
            return self.report(document)
        active = self.active()
        if active and active[1]["transaction"] != transaction_id:
            raise ContractError("another durable transaction is active")
        try:
            current = self.current_release()
            current_commit = read_json(current / "bundle-manifest.json")["source_commit"]
        except FileNotFoundError:
            # The approved legacy-adoption rename can be interrupted before
            # its current symlink is rewritten. Only the exact checkpoint's
            # old pointer may be repaired, never an arbitrary dangling link.
            link = self.install / "current"
            raw = os.readlink(link)
            target = Path(os.path.abspath(link.parent / raw))
            if target != Path(document["previous_release"]):
                raise ContractError("unrecognized dangling current pointer")
            self.previous_release(document)
            current_commit = document["previous_commit"]
        if current_commit not in {document["previous_commit"], document["candidate_commit"]}:
            raise ContractError("checkpoint does not belong to current release lineage")
        previous = self.previous_release(document)
        private_file(path / "compose.json")
        if hash_file(path / "compose.json") != document["compose_sha256"]:
            raise ContractError("checkpoint Compose checksum mismatch")
        config = read_json(path / "compose.json")
        self.runtime.verify_images(config)
        preparing = document["phase"] == "PREPARING"
        if not preparing:
            if set(document["artifacts"]) != set(COMPONENTS) or any(document["artifacts"].get(name) is None for name in ("postgres", "qdrant")):
                raise ContractError("durable backup is incomplete")
            # Validate every archive before stopping or replacing any live data.
            for name, row in document["artifacts"].items():
                if row is None:
                    continue
                if row.get("file") != name + ".tar":
                    raise ContractError("invalid checkpoint artifact path")
                archive = path / row["file"]
                private_file(archive)
                if archive.stat().st_size != row["size_bytes"] or hash_file(archive) != row["sha256"]:
                    raise ContractError("durable archive checksum/size mismatch")
        atomic_json(self.active_path, {"transaction": transaction_id})
        self.runtime.stop(self.data, require_clean=False)
        if not preparing:
            extraction = self.data / (".restore-" + transaction_id)
            secure_directory(extraction, create=True)
            if shutil.disk_usage(self.data).free < sum(row["size_bytes"] for row in document["artifacts"].values() if row) + 64 * 1024**2:
                raise ContractError("insufficient restore space; no live directory was replaced")
            # Fully extract both durable stores before the first rename. Failed
            # candidate data is quarantined, not deleted. Resume recognizes
            # completed renames, including a crash between rename and journal.
            for name, row in document["artifacts"].items():
                if name in document["restored"] or row is None:
                    continue
                staged = extraction / name
                parked = self.data / (".failed-" + transaction_id + "-" + name)
                if parked.exists() and (self.data / name).exists() and not staged.exists():
                    if not archive_matches(path / row["file"], self.data, name):
                        raise ContractError("interrupted restore destination does not match checkpoint")
                    document["restored"].append(name)
                    self.save(path, document, "RESTORING")
                    continue
                if staged.exists():
                    regular_files(staged)
                    shutil.rmtree(staged)
                extract_tree(path / row["file"], extraction, name)
            self.save(path, document, "RESTORING")
            for name, row in document["artifacts"].items():
                if name in document["restored"]:
                    continue
                live = self.data / name
                parked = self.data / (".failed-" + transaction_id + "-" + name)
                no_links(live)
                no_links(parked)
                if not parked.exists() and live.exists():
                    live.rename(parked)
                    fsync_directory(self.data)
                if row is not None:
                    staged = extraction / name
                    if live.exists():
                        raise ContractError("restore destination unexpectedly exists")
                    staged.rename(live)
                    fsync_directory(self.data)
                document["restored"].append(name)
                self.save(path, document)
        tmp = self.install / (".current-restore-" + transaction_id)
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(previous)
        os.replace(tmp, self.install / "current")
        fsync_directory(self.install)
        for name, value in document["release_markers"].items():
            if name not in {".offline-inputs-required", "active-release"}:
                raise ContractError("unknown checkpoint release marker")
            marker = self.install / name
            no_links(marker)
            if value is None:
                marker.unlink(missing_ok=True)
            else:
                temporary = path / "marker.tmp"
                temporary.write_bytes(base64.b64decode(value, validate=True))
                os.chmod(temporary, 0o600)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, marker)
        fsync_directory(self.install)
        # The previous rendered config uses local image IDs, so this cannot
        # fetch dependencies, rebuild old source, or run a newer DB binary.
        runtime_path = path / "compose.json"
        if str(previous) != document["previous_release"]:
            runtime_path = path / "restore-runtime.json"
            translated = json.loads(json.dumps(config).replace(
                document["previous_release"] + "/", str(previous) + "/"))
            atomic_json(runtime_path, translated)
        self.runtime.start(runtime_path)
        self.save(path, document, "ABORTED" if preparing else "RESTORED")
        self.active_path.unlink(missing_ok=True)
        fsync_directory(self.root)
        return self.report(document)

    def assert_ready(self, candidate_commit: str) -> None:
        active = self.active()
        if active:
            checkpoint, doc = active
            if doc["phase"] != "READY" or doc["candidate_commit"] != candidate_commit:
                raise ContractError("RECOVERY_REQUIRED: candidate is not covered by a ready checkpoint")
            current = self.current_release()
            current_commit = read_json(current / "bundle-manifest.json")["source_commit"]
            if current_commit not in {doc["previous_commit"], doc["candidate_commit"]}:
                raise ContractError("ready checkpoint does not belong to the current release lineage")
            if set(doc["artifacts"]) != set(COMPONENTS) or any(doc["artifacts"].get(name) is None for name in ("postgres", "qdrant")):
                raise ContractError("durable backup is incomplete")
            self.previous_release(doc)
            private_file(checkpoint / "compose.json")
            if hash_file(checkpoint / "compose.json") != doc["compose_sha256"]:
                raise ContractError("checkpoint runtime checksum mismatch")
            self.runtime.verify_images(read_json(checkpoint / "compose.json"))
            for name, row in doc["artifacts"].items():
                if row is None:
                    continue
                if row.get("file") != name + ".tar":
                    raise ContractError("invalid checkpoint artifact path")
                archive = checkpoint / row["file"]
                private_file(archive)
                if archive.stat().st_size != row["size_bytes"] or hash_file(archive) != row["sha256"]:
                    raise ContractError("candidate checkpoint archive checksum/size mismatch")
            return
        # A receipt permits restarting the CURRENT accepted release, not
        # switching to an old accepted source while retaining a newer schema.
        receipt = self.root / (candidate_commit + ".accepted.json")
        if receipt.is_file():
            private_file(receipt)
            current = self.current_release()
            if (read_json(receipt).get("source_commit") == candidate_commit
                    and read_json(current / "bundle-manifest.json")["source_commit"] == candidate_commit):
                return
        empty = True
        for name in ("postgres", "qdrant"):
            directory = no_links(self.data / name)
            if directory.exists() and not directory.is_dir():
                raise ContractError("durable storage path is not a directory")
            empty = empty and not any(directory.glob("*"))
        if empty:
            return  # genuinely empty fresh installation
        raise ContractError("DURABLE_CHECKPOINT_REQUIRED: refusing an unbacked candidate")

    def complete(self, candidate_commit: str) -> dict:
        active = self.active()
        if not active or active[1]["phase"] != "READY" or active[1]["candidate_commit"] != candidate_commit:
            raise ContractError("no matching ready checkpoint to complete")
        current = self.current_release()
        if read_json(current / "bundle-manifest.json")["source_commit"] != candidate_commit:
            raise ContractError("candidate is not current")
        path, document = active
        self.runtime.enable_restart()
        atomic_json(self.root / (candidate_commit + ".accepted.json"), {"source_commit": candidate_commit})
        self.save(path, document, "COMMITTED")
        self.active_path.unlink()
        fsync_directory(self.root)
        return self.report(document)

    def accept_fresh(self, candidate_commit: str) -> None:
        if self.active():
            return
        current = self.current_release()
        if read_json(current / "bundle-manifest.json")["source_commit"] != candidate_commit:
            raise ContractError("fresh acceptance does not match current release")
        atomic_json(self.root / (candidate_commit + ".accepted.json"), {"source_commit": candidate_commit})

    @staticmethod
    def report(document: dict) -> dict:
        return {key: document[key] for key in ("format", "version", "transaction", "phase", "previous_commit", "candidate_commit", "recovery_entrypoint")}


@contextlib.contextmanager
def operation_lock(install: Path, inherited: int | None):
    path = install / ".operation.lock"
    no_links(path)
    if inherited is not None:
        private_file(path)
        actual, expected = os.fstat(inherited), path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ContractError("inherited operation lock descriptor does not match installation")
        # This must be the inherited open-file description, not just an env flag.
        fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    else:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private_file(path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["begin", "restore", "recover", "status", "assert-ready", "complete", "accept-fresh"])
    parser.add_argument("--install-root", type=Path, default=Path("/opt/aoitalk"))
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/aoitalk"))
    parser.add_argument("--project", default="aoitalk-enterprise")
    parser.add_argument("--candidate")
    parser.add_argument("--transaction")
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--compose-stdin", action="store_true", help="optional pre-rendered previous Compose config; default captures the running containers")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ContractError("durable-state operations require root")
        if args.install_root != Path("/opt/aoitalk") or args.data_root != Path("/var/lib/aoitalk") or args.project != "aoitalk-enterprise":
            raise ContractError("durable-state CLI requires the canonical managed installation")
        if args.command in {"begin", "assert-ready", "complete", "accept-fresh"} and not COMMIT.fullmatch(args.candidate or ""):
            raise ContractError("a full candidate source commit is required")
        with operation_lock(secure_directory(args.install_root), args.lock_fd):
            store = Checkpoints(args.install_root, args.data_root, DockerRuntime(args.project))
            if args.command == "begin":
                config = json.load(sys.stdin) if args.compose_stdin else {}
                result = store.begin(args.candidate, config)
            elif args.command in {"recover", "restore"}:
                active = store.active()
                transaction_id = args.transaction or (active[1]["transaction"] if active else "")
                result = store.restore(transaction_id)
            elif args.command == "complete":
                result = store.complete(args.candidate)
            elif args.command == "assert-ready":
                store.assert_ready(args.candidate)
                result = {"status": "ready"}
            elif args.command == "accept-fresh":
                store.accept_fresh(args.candidate)
                result = {"status": "accepted"}
            else:
                active = store.active()
                result = store.report(active[1]) if active else {"phase": "IDLE"}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ContractError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        # No rendered Compose, database contents, or credentials are printed.
        print(f"Enterprise durable-state failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
