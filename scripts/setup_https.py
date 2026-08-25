#!/usr/bin/env python3
"""Obtain or renew the AoiTalk certificate with lego and DDNS Now DNS-01."""

import argparse
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
LEGO = PROJECT_ROOT / "caddy" / "lego.exe"
CERT_DIR = PROJECT_ROOT / "certs"


def load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--renew", action="store_true")
    args = parser.parse_args()
    load_env()

    domain = os.environ.get("DDNS_NOW_DOMAIN", "")
    password = os.environ.get("DDNS_NOW_PASSWORD", "")
    if not LEGO.exists() or not domain or not password:
        raise SystemExit("lego or DDNS Now configuration is missing")

    fqdn = f"{domain}.f5.si"
    env = os.environ.copy()
    env["EXEC_PATH"] = str(PROJECT_ROOT / "scripts" / "ddns_acme_exec.bat")
    env["EXEC_PROPAGATION_TIMEOUT"] = "300"
    env["EXEC_POLLING_INTERVAL"] = "10"
    command = [
        str(LEGO),
        "--dns",
        "exec",
        "--domains",
        fqdn,
        "--email",
        f"admin@{fqdn}",
        "--path",
        str(CERT_DIR),
    ]
    if args.renew:
        command.extend(["renew", "--days", "30", "--no-random-sleep"])
    else:
        command.extend(["--accept-tos", "run"])
    raise SystemExit(subprocess.run(command, cwd=PROJECT_ROOT, env=env).returncode)


if __name__ == "__main__":
    main()
