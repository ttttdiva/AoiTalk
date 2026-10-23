"""Compare migrated module provenance with the target files in the 80 Git HEAD."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
SOURCE_ROOT = REPOSITORY_ROOT.parent / "80_HomeMCPGateway"
MANIFEST = json.loads((PACKAGE_ROOT / "migration_manifest.json").read_text(encoding="utf-8"))


def _git_show(path: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"HEAD:{path}"],
        cwd=SOURCE_ROOT,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    if not (SOURCE_ROOT / ".git").exists():
        print("SKIP: sibling 80_HomeMCPGateway checkout is absent; standalone package remains usable.")
        return 0

    failures: list[str] = []
    for name, record in MANIFEST["source_files"].items():
        path = f"home_mcp_gateway/{name}"
        try:
            source_hash = hashlib.sha256(_git_show(path)).hexdigest()
        except subprocess.CalledProcessError:
            failures.append(f"80 HEAD file missing: {path}")
            continue
        if source_hash != record["source_sha256"]:
            failures.append(f"80 target module changed: {path}")

    fixture_path = "tests/fixtures/aoitalk_41_management_contract.v3.json"
    try:
        fixture_hash = hashlib.sha256(_git_show(fixture_path)).hexdigest()
    except subprocess.CalledProcessError:
        failures.append(f"80 fixture missing: {fixture_path}")
    else:
        if fixture_hash != MANIFEST["management_contract_fixture_sha256"]:
            failures.append(f"80 management contract fixture changed: {fixture_path}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("PASS: migrated 80 source modules and management fixture match their recorded target-file SHA-256 values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
