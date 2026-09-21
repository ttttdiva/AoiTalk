"""Run only inside disposable, pinned linux/amd64 producer containers."""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True  # Never mutate the checksum-bound source/release.

import argparse
import email
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from urllib.request import build_opener
from offline_registry_source import HTTPSRedirect
import zipfile

from enterprise_release_common import ContractError, atomic_json, file_records, hash_file, read_json


def run(args: list[str], **kwargs) -> str:
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, **kwargs)
    return result.stdout


def phases_from_dockerfile(text: str) -> dict[str, list[str]]:
    blocks = re.findall(r"apt-get install -y --no-install-recommends\s+([^;]+);", text)
    if len(blocks) != 2:
        raise ContractError("Dockerfile APT definition changed; update the official producer parser")
    groups = [block.replace("\\\n", " ").split() for block in blocks]
    for names in groups:
        if not names or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) for name in names):
            raise ContractError("Dockerfile APT definition is not an exact package-name list")
    if not re.search(r"apt-get install -y nodejs\s*;", text):
        raise ContractError("Dockerfile NodeSource phase changed")
    return {"builder": groups[0], "runtime": groups[1], "nodejs": sorted(set(groups[1]) | {"nodejs"})}


def parse_control(text: str) -> dict[str, str]:
    result, key = {}, None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            result[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            result[key] = value.strip()
    return result


def collect_apt(source: Path, root: Path, manifest: dict) -> dict:
    phases = phases_from_dockerfile((source / "Dockerfile").read_text())
    provenance = root / "provenance" / "apt-indexes"
    provenance.mkdir(parents=True)
    archives = root / "apt" / "archives"
    archives.mkdir(parents=True)
    lists = root / "apt" / "lists"
    lists.mkdir(parents=True)
    nodesource = root / "nodesource"
    nodesource.mkdir()
    with tempfile.TemporaryDirectory(prefix="enterprise-apt-") as raw:
        scratch = Path(raw)
        original_status = scratch / "base-status"
        shutil.copyfile("/var/lib/dpkg/status", original_status)
        # Disable Docker's cache-cleaning hook inside this disposable container,
        # never on the producer host or on the deployment target.
        Path("/etc/apt/apt.conf.d/docker-clean").unlink(missing_ok=True)
        Path("/etc/apt/apt.conf.d/99enterprise-collect").write_text(
            'APT::Keep-Downloaded-Packages "true";\nAcquire::GzipIndexes "false";\n')
        run(["bash", str(source / "docker/ensure-https-apt-sources.sh")])
        run(["apt-get", "update", "-o", "APT::Update::Error-Mode=any"])
        run(["apt-get", "install", "-y", "--no-install-recommends", "gnupg", "ca-certificates"])
        setup = manifest["build_reproducibility"]["nodesource_setup"]
        if setup["url"] != "https://deb.nodesource.com/setup_22.x":
            raise ContractError("unsupported NodeSource setup definition")
        # The pinned setup script is provenance only. Do not execute a mutable
        # setup endpoint; import the official public key and configure the
        # current, official nodistro repository explicitly.
        for url, path in [
            (setup["url"], nodesource / "setup_22.x"),
            ("https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key", scratch / "nodesource-public.asc"),
        ]:
            with build_opener(HTTPSRedirect()).open(url, timeout=120) as response:
                if not response.url.startswith("https://"):
                    raise ContractError("NodeSource acquisition left HTTPS")
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ContractError("NodeSource metadata exceeds limit")
            path.write_bytes(payload)
        if hash_file(nodesource / "setup_22.x") != setup["sha256"]:
            raise ContractError("NodeSource setup SHA256 changed; update the reviewed source pin, not the lockfile")
        run(["gpg", "--batch", "--yes", "--dearmor", "--output", str(nodesource / "nodesource.gpg"), str(scratch / "nodesource-public.asc")])
        sources_line = "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main\n"
        (nodesource / "sources.list").write_text(sources_line)
        shutil.copyfile(nodesource / "nodesource.gpg", "/usr/share/keyrings/nodesource.gpg")
        os.chmod("/usr/share/keyrings/nodesource.gpg", 0o644)
        Path("/etc/apt/sources.list.d/nodesource.list").write_text(sources_line)
        Path("/etc/apt/preferences.d/enterprise-nodejs").write_text("Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n")
        run(["bash", str(source / "docker/ensure-https-apt-sources.sh")])
        run(["apt-get", "update", "-o", "APT::Update::Error-Mode=any"])
        # apt verifies the upstream InRelease/Release signatures before these
        # indexes are copied. Retain those exact acquired bytes for provenance.
        indexed: dict[tuple[str, str, str], tuple[dict, str]] = {}
        for path in sorted(Path("/var/lib/apt/lists").iterdir()):
            if not path.is_file() or path.name == "lock":
                continue
            saved = provenance / path.name
            shutil.copyfile(path, saved)
            if "_Packages" not in path.name:
                continue
            text = run(["/usr/lib/apt/apt-helper", "cat-file", str(path)])
            for block in re.split(r"\n\s*\n", text):
                record = parse_control(block)
                key = tuple(record.get(field, "") for field in ("Package", "Version", "Architecture"))
                if all(key):
                    indexed.setdefault(key, (record, block))
        packages: dict[str, dict] = {}
        blocks: dict[str, str] = {}
        for phase, requested in phases.items():
            phase_root = scratch / phase
            (phase_root / "partial").mkdir(parents=True)
            # Resolve against the pristine pinned base status, not the host or
            # the collector's gnupg/tooling installation.
            run(["apt-get", "--download-only", "--reinstall", "-y", "--no-install-recommends",
                 "-o", f"Dir::State::status={original_status}", "-o", f"Dir::Cache::archives={phase_root}",
                 "install", *requested])
            for deb in phase_root.glob("*.deb"):
                control = parse_control(run(["dpkg-deb", "--field", str(deb)]))
                key = tuple(control.get(field, "") for field in ("Package", "Version", "Architecture"))
                if key not in indexed or key[2] not in {"amd64", "all"}:
                    raise ContractError("acquired .deb is not covered by authenticated APT indexes")
                record, block = indexed[key]
                filename = Path(record["Filename"]).name
                target = archives / filename
                size, digest = deb.stat().st_size, hash_file(deb)
                if int(record["Size"]) != size or record.get("SHA256") != digest:
                    raise ContractError("authenticated APT index/.deb mismatch")
                if key[0] in packages:
                    row = packages[key[0]]
                    if (row["version"], row["architecture"], row["sha256"]) != (key[1], key[2], digest):
                        raise ContractError("conflicting APT versions between Dockerfile phases")
                    row["phases"].append(phase)
                    continue
                if target.exists():
                    raise ContractError("APT archive filename collision")
                shutil.copyfile(deb, target)
                packages[key[0]] = {"name": key[0], "version": key[1], "architecture": key[2],
                                    "archive": "apt/archives/" + filename, "sha256": digest,
                                    "size_bytes": size, "phases": [phase]}
                blocks[key[0]] = block.strip()
            if not set(requested).issubset(packages):
                raise ContractError("APT acquisition omitted a requested/reinstalled package")
        # A narrowed Packages file avoids ambiguous duplicate records from
        # multiple Debian suites. Each record remains byte-for-byte upstream;
        # its authenticated original index is retained under provenance/.
        index = lists / "Packages"
        index.write_text("\n\n".join(blocks[name] for name in sorted(blocks)) + "\n", encoding="utf-8")
    rows = [packages[name] for name in sorted(packages)]
    apt = {"network_required": False, "install_mode": "no-download", "required_names": sorted(packages),
           "packages": rows, "indexes": [{"path": "apt/lists/Packages", "size_bytes": index.stat().st_size, "sha256": hash_file(index)}],
           "package_indexes": [{"path": "apt/lists/Packages", "packages": rows}]}
    node = {"url": setup["url"], "sha256": setup["sha256"], "https_required": True,
            "path": "nodesource/setup_22.x", "sources_list": "nodesource/sources.list", "keyring": "nodesource/nodesource.gpg",
            "sources_list_sha256": hash_file(nodesource / "sources.list"), "keyring_sha256": hash_file(nodesource / "nodesource.gpg")}
    atomic_json(root / "offline-build-manifest.json", {"apt": apt})
    return {"apt": apt, "nodesource": node}


def wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA") and name.count("/") == 1]
        if len(names) != 1:
            raise ContractError("wheel lacks unique distribution metadata")
        metadata = email.message_from_bytes(archive.read(names[0]))
    name, version = metadata.get("Name", ""), metadata.get("Version", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not version or any(c.isspace() for c in version):
        raise ContractError("invalid wheel Name/Version")
    return re.sub(r"[-_.]+", "-", name).lower(), version


def collect_python(source: Path, root: Path) -> dict:
    run(["bash", str(source / "docker/install-enterprise-system-deps.sh"), str(root), "builder"])
    project = tomllib.loads((source / "pyproject.toml").read_text())
    name = re.sub(r"[-_.]+", "-", project["project"]["name"]).lower()
    wheelhouse, locks = root / "python-wheels", root / "python"
    wheelhouse.mkdir()
    locks.mkdir()
    with tempfile.TemporaryDirectory(prefix="enterprise-wheels-") as raw:
        scratch = Path(raw)
        working = scratch / "project"
        shutil.copytree(source, working)
        if not (working / "README.md").exists():
            shutil.copyfile(working / "README.enterprise.md", working / "README.md")
        selected: dict[str, dict] = {}
        pip = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "wheel", "--index-url", "https://pypi.org/simple", "--no-cache-dir"]
        for kind, requirements in (("build", project["build-system"]["requires"]), ("runtime", [str(working)])):
            directory = scratch / kind
            directory.mkdir()
            run([*pip, "--wheel-dir", str(directory), *requirements])
            entries = []
            for wheel in sorted(directory.glob("*.whl")):
                distribution, version = wheel_identity(wheel)
                if distribution == name:
                    continue  # application is always rebuilt from sanitized source
                digest = hash_file(wheel)
                identity = (version, digest)
                if distribution in selected and selected[distribution]["identity"] != identity:
                    raise ContractError("conflicting Python wheel resolutions between build/runtime")
                selected[distribution] = {"identity": identity, "path": wheel.name}
                target = wheelhouse / wheel.name
                if not target.exists():
                    shutil.copyfile(wheel, target)
                elif hash_file(target) != digest:
                    raise ContractError("wheel filename collision")
                entries.append(f"{distribution}=={version} --hash=sha256:{digest}\n")
            if not entries:
                raise ContractError("empty Python dependency closure")
            (locks / f"{kind}-requirements.lock").write_text("".join(sorted(entries)), encoding="utf-8")
    return {"wheelhouse": "python-wheels", "no_index": True, "require_hashes": True,
            "pyproject_sha256": hash_file(source / "pyproject.toml"),
            "build_requirements_lock": "python/build-requirements.lock", "runtime_requirements_lock": "python/runtime-requirements.lock",
            "wheels": [{"path": "python-wheels/" + r["path"], "sha256": r["sha256"]} for r in file_records(wheelhouse)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["apt", "python"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "apt":
        result = collect_apt(args.source, args.root, read_json(args.handoff))
    else:
        result = {"python": collect_python(args.source, args.root)}
    atomic_json(args.root / f"{args.phase}-collection.json", result)


if __name__ == "__main__":
    main()
