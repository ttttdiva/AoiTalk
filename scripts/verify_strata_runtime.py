"""Read-only verification of the pinned Strata source and optional prepared model."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm.strata_manifest import STRATA_MANIFEST
from src.services.strata_runtime import (
    strata_prepared_config,
    verify_strata_engine,
    verify_strata_source,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="Also verify prepared artifacts and engine --version")
    args = parser.parse_args()
    root = args.source_root.resolve()
    try:
        verify_strata_source(root)
        observed = None
        if args.config:
            config_path = args.config.resolve()
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            prepared = strata_prepared_config({
                "source_root": str(root),
                "platform_supported": sys.platform.startswith(("win", "linux")),
                "python": str(root / (".venv/Scripts/python.exe" if sys.platform.startswith("win") else ".venv/bin/python")),
                "config_path": str(config_path),
                "model_alias": payload["model_name"],
            })
            observed = verify_strata_engine(prepared)
        print(json.dumps({
            "manifest": asdict(STRATA_MANIFEST),
            "source_verified": True,
            "engine_version_observed": observed,
        }, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Strata検証失敗: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
