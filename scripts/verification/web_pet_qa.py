"""Run Web pet QA against an isolated, authenticated Next/FastAPI runtime.

Use the repository Python environment with test dependencies installed:
  python scripts/verification/web_pet_qa.py prepare
  python scripts/verification/web_pet_qa.py verify --manifest <printed path>
  python scripts/verification/web_pet_qa.py cleanup --manifest <printed path>

Reuses the existing isolated PostgreSQL/source-snapshot lifecycle. It never
launches main.py, edits the normal database, or touches data/web-pets in the
working repository. Credentials come only from the root .env.qa-login.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.verification import ai_employee_qa as qa


def pet_environment(manifest: dict, private: dict) -> dict[str, str]:
    env = BASE_ENVIRONMENT(manifest, private)
    env.update(FEATURE_ENTERTAINMENT="true",
        AOITALK_PET_DATA_DIR=str(Path(manifest["output_dir"]) / "data" / "web-pets"))
    return env


BASE_ENVIRONMENT = qa.child_environment


def browser(manifest: dict, phase: str, second_browser_channel: str | None = None) -> None:
    root = Path(manifest["output_dir"])
    frontend = root / "frontend"
    # Keep the spec in the same snapshot as the application being verified.
    config = frontend / "pets-qa.config.cjs"
    config.write_text("module.exports={testDir:'./e2e-live',testMatch:'pets.live.spec.ts',"
        "workers:1,reporter:'list',use:{browserName:'chromium',trace:'off'}}", encoding="utf-8")
    env = pet_environment(manifest, qa.private_state(manifest))
    username, password = qa.read_qa_credentials()
    env.update(PET_QA_BASE_URL=manifest["frontend_url"], PET_QA_USERNAME=username,
        PET_QA_PASSWORD=password, PET_QA_DIRECTORY=str(root), PET_QA_PHASE=phase)
    if second_browser_channel:
        env["PET_QA_SECOND_BROWSER_CHANNEL"] = second_browser_channel
    qa.run_logged([manifest["node"], frontend / "node_modules/@playwright/test/cli.js", "test", "-c", config],
        cwd=frontend, env=env, log=root / f"pets-browser-{phase}.log", timeout=300)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "register", "restarted", "verify", "stop", "cleanup"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--second-browser-channel", choices=("chrome", "msedge"))
    parser.add_argument("--postgres-bin", default=r"C:\Program Files\PostgreSQL\16\bin")
    args = parser.parse_args()
    qa.child_environment = pet_environment
    if args.command == "prepare":
        args.provider_mode = "succeeded"
        print(qa.prepare(args))
        return
    if not args.manifest:
        parser.error("--manifest is required")
    manifest = qa.load_manifest(args.manifest)
    if args.command == "verify":
        if not manifest["processes"]:
            qa.start(manifest)
        browser(qa.load_manifest(args.manifest), "register", args.second_browser_channel)
        qa.stop(qa.load_manifest(args.manifest))
        qa.start(qa.load_manifest(args.manifest))
        browser(qa.load_manifest(args.manifest), "restarted", args.second_browser_channel)
    elif args.command in ("register", "restarted"):
        browser(manifest, args.command, args.second_browser_channel)
    else:
        {"start": qa.start, "stop": qa.stop, "cleanup": qa.cleanup}[args.command](manifest)
    print(f"Web pet QA {args.command}: complete")


if __name__ == "__main__":
    main()
