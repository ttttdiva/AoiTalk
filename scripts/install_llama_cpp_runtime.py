"""Install an official runtime separately; never download/load models or edit config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.local_llm_runtime_manager import ManagedLocalRuntimeManager  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--release", default="b11541")
    parser.add_argument("--backend", choices=["vulkan", "cpu"], default="vulkan")
    parser.add_argument(
        "--model", default="hemmingway-1", help="Trusted profile for build validation"
    )
    args = parser.parse_args(argv)
    if not args.runtime_root.is_absolute():
        parser.error("--runtime-root must be an absolute, separate external directory")
    try:
        result = ManagedLocalRuntimeManager(
            root=args.runtime_root
        ).install_llama_cpp_runtime_only(
            args.model,
            release_tag=args.release,
            backend=args.backend,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"installed": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
