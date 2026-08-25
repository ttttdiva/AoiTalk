#!/usr/bin/env python3
"""Manage DDNS Now ACME challenge records for lego's exec DNS provider."""

import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def load_env() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def call_ddns_api(domain: str, password: str, acme_value: str) -> str:
    params = urllib.parse.urlencode(
        {
            "domain": domain,
            "password": password,
            "acme": acme_value,
            "format": "json",
        }
    )
    request = urllib.request.Request(f"https://f5.si/update.php?{params}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(f"Usage: {sys.argv[0]} <present|cleanup> <fqdn> [value]")

    action = sys.argv[1]
    fqdn = sys.argv[2]
    value = sys.argv[3] if len(sys.argv) > 3 else ""
    load_env()

    domain = os.environ.get("DDNS_NOW_DOMAIN", "")
    password = os.environ.get("DDNS_NOW_PASSWORD", "")
    if not domain or not password:
        raise SystemExit("DDNS_NOW_DOMAIN / DDNS_NOW_PASSWORD is not configured")

    if action == "present":
        print(f"[ACME] Setting TXT record: {fqdn}")
        print(call_ddns_api(domain, password, value))
    elif action == "cleanup":
        print(f"[ACME] Clearing TXT record: {fqdn}")
        print(call_ddns_api(domain, password, ""))
    else:
        raise SystemExit(f"Unknown action: {action}")


if __name__ == "__main__":
    main()
