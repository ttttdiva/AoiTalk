"""Snapshot only the editor entry assets missing from the runtime distribution.

Run after updating resources/seethrough-rig/release.json:
  venv/bin/python scripts/snapshot_seethrough_editor_fixture.py --source-repo PATH

Reads the pinned Git revision, never an upstream working tree or the network.
The fixture plus the checked-in runtime forms the real static editor graph.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("editor.html", "css/editor.css", "js/editor.js", "js/standalone-settings.js")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    args = parser.parse_args()
    release = json.loads((ROOT / "resources/seethrough-rig/release.json").read_text(encoding="utf-8"))
    fixture = {"repository": release["repository"], "revision": release["revision"], "files": {}}
    for name in FILES:
        payload = subprocess.run(
            ["git", "show", f"{release['revision']}:web/{name}"],
            cwd=args.source_repo, check=True, capture_output=True, timeout=15,
        ).stdout
        fixture["files"][name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "source": payload.decode("utf-8"),
        }
    destination = ROOT / "tests/fixtures/seethrough_editor_entries.json"
    destination.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
