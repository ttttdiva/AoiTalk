#!/usr/bin/env python3
"""Check/render the canonical Enterprise secret contract.

The Compose file intentionally keeps explicit ``*_FILE`` entries so operators
can audit it without executing a generator.  This tool is the deterministic
source-of-truth check: it renders the expected fragment from
``secret-schema.json`` and fails if Compose or the root entrypoint drifts.
Only names and metadata are ever printed; secret bytes are not read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the Enterprise image includes PyYAML
    yaml = None


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "deploy" / "enterprise" / "secret-schema.json"
COMPOSE_PATH = ROOT / "deploy" / "enterprise" / "compose.yml"
ENTRYPOINT_PATH = ROOT / "docker" / "entrypoint.enterprise.sh"
MARKER = "secret-contract: generated from secret-schema.json"


def _schema() -> dict[str, Any]:
    try:
        document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("secret schema cannot be loaded") from exc
    if not isinstance(document, dict) or not isinstance(document.get("secrets"), list):
        raise ValueError("secret schema is invalid")
    return document


def _environment(service: dict[str, Any]) -> dict[str, str]:
    values = service.get("environment", {})
    if isinstance(values, dict):
        return {str(key): str(value) for key, value in values.items()}
    if isinstance(values, list):
        result: dict[str, str] = {}
        for entry in values:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                result[key] = value
        return result
    return {}


def _compose() -> dict[str, Any]:
    if yaml is None:
        raise ValueError("PyYAML is required to check the Compose contract")
    try:
        document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Enterprise Compose contract cannot be loaded") from exc
    if not isinstance(document, dict):
        raise ValueError("Enterprise Compose contract is invalid")
    return document


def _expected_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    specs = document["secrets"]
    if not all(isinstance(item, dict) for item in specs):
        raise ValueError("secret schema contains an invalid entry")
    result: list[dict[str, Any]] = []
    seen_env: set[str] = set()
    seen_file: set[str] = set()
    for item in specs:
        env_name = item.get("env")
        file_name = item.get("file")
        if not isinstance(env_name, str) or not isinstance(file_name, str):
            raise ValueError("secret schema contains an invalid name")
        if env_name in seen_env or file_name in seen_file:
            raise ValueError("secret schema contains duplicate names")
        seen_env.add(env_name)
        seen_file.add(file_name)
        result.append(item)
    return result


def _render_fragment(specs: list[dict[str, Any]]) -> str:
    lines = ["# Generated from secret-schema.json; do not edit by hand.", "environment:"]
    for spec in specs:
        lines.append(f"  {spec['env']}_FILE: /run/secrets/{spec['file']}")
    lines.append("secrets:")
    for spec in specs:
        lines.append(f"  - {spec['file']}")
    return "\n".join(lines) + "\n"


def check_contract() -> None:
    document = _schema()
    specs = _expected_specs(document)
    compose = _compose()
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    entrypoint_text = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    if MARKER not in compose_text or MARKER not in entrypoint_text:
        raise ValueError("Compose/entrypoint is missing the generated secret marker")

    services = compose.get("services")
    if not isinstance(services, dict) or not isinstance(services.get("aoitalk"), dict):
        raise ValueError("Enterprise Compose aoitalk service is missing")
    app_env = _environment(services["aoitalk"])
    app_secrets = services["aoitalk"].get("secrets", [])
    app_secret_names = {
        item if isinstance(item, str) else item.get("source")
        for item in app_secrets
        if isinstance(item, str) or isinstance(item, dict)
    }
    top_secrets = compose.get("secrets")
    if not isinstance(top_secrets, dict):
        raise ValueError("Enterprise Compose top-level secrets are missing")

    env_names = {spec["env"] for spec in specs}
    file_names = {spec["file"] for spec in specs}
    for spec in specs:
        env_name = spec["env"]
        file_name = spec["file"]
        expected_path = f"/run/secrets/{file_name}"
        if env_name in app_env:
            raise ValueError(f"plaintext secret environment is present: {env_name}")
        if app_env.get(f"{env_name}_FILE") != expected_path:
            raise ValueError(f"Compose *_FILE mapping drift: {env_name}")
        if file_name not in app_secret_names:
            raise ValueError(f"Compose service secret mount is missing: {file_name}")
        source = top_secrets.get(file_name)
        if not isinstance(source, dict) or not str(source.get("file", "")).endswith(
            f"/{file_name}"
        ):
            raise ValueError(f"Compose secret source drift: {file_name}")

    provider_contract = document.get("providers", {})
    if not isinstance(provider_contract, dict):
        raise ValueError("provider secret contract is invalid")
    for provider, details in provider_contract.items():
        if not isinstance(details, dict):
            raise ValueError(f"provider contract is invalid: {provider}")
        names = [details.get("env"), *(details.get("aliases", []) or [])]
        if any(name not in env_names for name in names if isinstance(name, str)):
            raise ValueError(f"provider secret contract is not in schema: {provider}")

    # The launcher must remain metadata-driven; these checks prevent a future
    # hand-written list from silently diverging from the JSON source.
    required_fragments = (
        "SCHEMA_PATH=",
        "schema_rows=",
        'unset "$env_file_name"',
        "stat -c '%u:%g:%a'",
        "export HOME=/home/aoitalk",
        "export USER=aoitalk",
        "export LOGNAME=aoitalk",
        "XDG_CACHE_HOME=/app/cache/.cache",
    )
    for fragment in required_fragments:
        if fragment not in entrypoint_text:
            raise ValueError(f"entrypoint secret boundary fragment is missing: {fragment}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check Compose/entrypoint parity against secret-schema.json",
    )
    parser.add_argument(
        "--render-compose-fragment",
        action="store_true",
        help="render deterministic Compose environment/secrets metadata",
    )
    args = parser.parse_args(argv)
    if not args.check and not args.render_compose_fragment:
        args.check = True
    try:
        document = _schema()
        specs = _expected_specs(document)
        if args.render_compose_fragment:
            sys.stdout.write(_render_fragment(specs))
        if args.check:
            check_contract()
            if not args.render_compose_fragment:
                print("Enterprise secret contract: OK")
    except ValueError as exc:
        print(f"Enterprise secret contract check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
