"""Assemble exact reviewed inputs; never turn notice assembly into legal approval."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pc_bridge.browser_backend import load_helper_bundle  # noqa: E402
from pc_bridge.agent_browser_supervisor import AgentBrowserPreparationError  # noqa: E402
from pc_bridge.license_bundle import LicenseBundleError, load_bundle  # noqa: E402


def canonical_text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def prepare_bundle(seed: Path, helper: Path, destination: Path, executable: Path | None = None) -> dict:
    """Copy only hash-checked inputs into a new repo-local candidate directory."""
    manifest, files = load_bundle(seed)
    bundle = load_helper_bundle(helper.absolute(), require_pinned=True)
    destination = destination.absolute()
    if ".." in destination.parts or not any(
            destination.is_relative_to(ROOT / name) for name in (".local", "artifacts")):
        raise LicenseBundleError("Notice output must be inside repository .local or artifacts")
    if destination.exists() or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
                                   for p in destination.parents):
        raise LicenseBundleError("Notice output already exists or has a linked parent")
    files.pop("manifest.json")
    kinds = {item["path"]: item["kind"] for item in manifest["files"]}
    originals = ROOT / "resources/agent-browser-bridge"
    policy_root = ROOT / "resources/pc-bridge-distribution"

    def add(path, raw, kind="notice"):
        files[path], kinds[path] = raw, kind

    add("application/LICENSE.txt", canonical_text(ROOT / "LICENSE"))
    from scripts.build_pc_bridge import HELPER_NOTICES
    for path in HELPER_NOTICES:
        raw = (originals / path).read_bytes()
        add("helper/" + path, raw)
    add("helper/build-manifest.json", bundle.build_manifest.read_bytes(), "inventory")
    for name in ("bridge.patch", "resulting-files.json", "upstream.json", "contract.json"):
        add("helper/source-inputs/" + name, canonical_text(originals / name), "source")
    for name, kind in (("Visual-C-V14-License-Redistributable_and_Runtime_ENU.docx", "source"),
                       ("Visual-C-V14-License-Redistributable_and_Runtime_ENU.txt", "notice")):
        add("official-originals/microsoft-vcredist-14.51.36247.0/" + name,
            (policy_root / name).read_bytes(), kind)
    add("distribution/notice-policy.json", canonical_text(policy_root / "notice-policy.json"), "inventory")
    add("helper/licenses/supplemental/originals.json",
        (originals / "licenses/supplemental/originals.json").read_bytes(), "inventory")
    if executable is not None:
        from scripts.build_pc_bridge import packaged_inventory
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(executable))
        inventory = json.loads(files[manifest["component_inventory"]])
        # CArchive's b type also labels staged data. Inspect PE bytes so data
        # labels cannot hide or invent native code in the recipient inventory.
        actual_native = {name.replace("\\", "/") for name in archive.toc
                         if archive.extract(name)[:2] == b"MZ"}
        if actual_native != {item["file"] for item in inventory["native_files"]}:
            raise LicenseBundleError("Actual native file set differs from reviewed originals")
        native_files = []
        for path in inventory["native_files"]:
            name = path["file"]
            archive_name = next((n for n in archive.toc if n.replace("\\", "/") == name), None)
            if archive_name is None:
                raise LicenseBundleError("Reviewed native component is absent: " + name)
            raw = archive.extract(archive_name)
            item = dict(path)
            sha = hashlib.sha256(raw).hexdigest()
            if name == "resources/agent-browser/agent-browser.exe":
                if sha != bundle.manifest.sha256:
                    raise LicenseBundleError("Frozen helper differs from pinned rebuild")
                item["official_original_matches"] = [{"origin": "current38-file annotated-source rebuild",
                                                       "sha256": sha}]
            elif sha != path["sha256"]:
                raise LicenseBundleError("Frozen native bytes differ from reviewed original: " + name)
            item.update(size_bytes=len(raw), sha256=sha)
            native_files.append(item)
        inventory.update(packaged_inventory=packaged_inventory(executable), native_files=native_files,
                         helper_sha256=bundle.manifest.sha256, rust_original_hashes_verified=433,
                         source_base_commit=json.loads(bundle.build_manifest.read_text(encoding="utf-8"))[
                             "source_base_checkpoint_commit"],
                         remaining=["Parent review of annotated source/helper and notice candidate",
                                    "Zune alternative decision and Microsoft redistribution authorization",
                                    "Actual GUI/Studio and product distribution remain unverified"])
        add(manifest["component_inventory"], (json.dumps(inventory, indent=2) + "\n").encode(), "inventory")
        manifest["packaged_inventory"] = inventory["packaged_inventory"]
    policy = json.loads(files["distribution/notice-policy.json"])
    for item in policy["required_files"]:
        if hashlib.sha256(files[item["path"]]).hexdigest() != item["sha256"]:
            raise LicenseBundleError("Required original differs from notice policy: " + item["path"])
    manifest["helper_sha256"] = bundle.manifest.sha256
    manifest["review_status"] = "pending_parent_release_review"
    manifest["distribution_checks"] = {
        "notice_assembly": "complete",
        "zune_license_choices": {},
        "zune_proposal": "Zlib for all five versions; awaiting parent decision",
        "microsoft_redistribution": {"status": "unconfirmed", "applicable_license": "",
                                      "authorization_evidence": ""},
        "release_review": "pending_parent_release_review",
    }
    manifest["files"] = [{"path": path, "kind": kinds[path], "size_bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
                          for path, raw in sorted(files.items())]
    destination.mkdir(parents=True, exist_ok=False)
    for path, raw in files.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    load_bundle(destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--helper-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory-executable", type=Path,
                        help="Audit actual onefile components against all reviewed native original hashes")
    args = parser.parse_args()
    try:
        manifest = prepare_bundle(args.seed, args.helper_bundle, args.output, args.inventory_executable)
    except (LicenseBundleError, AgentBrowserPreparationError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps({"status": manifest["review_status"], "files": len(manifest["files"]) + 1}))


if __name__ == "__main__":
    main()
