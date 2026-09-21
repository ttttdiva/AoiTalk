#!/usr/bin/env python3
"""Preview or execute a bounded verification-data cleanup manifest.

This is an operator-only wrapper around ``VerificationCleanupService``.
It intentionally has no SQL or name-based selection logic.  The coordinator
re-reads every manifest assertion and applies the repository's canonical
deletion/purge semantics.  Preview is the default; mutation requires all of
the following explicit inputs:

* ``--apply``;
* ``--manifest-id`` matching the loaded manifest exactly;
* ``--actor-user-id`` identifying the admin actor;
* ``--preview-digest`` copied from a fresh preview; and
* ``--confirm`` containing the exact confirmation phrase.

The script does not run at import time.  In particular, importing this module
for static validation or ``--help`` never opens a database session.

Examples (run from the repository root)::

    # Read-only inventory (safe default)
    python scripts/verification/cleanup_verification_data.py

    # The first command prints a digest.  Re-read it immediately before the
    # deliberately destructive second command.
    python scripts/verification/cleanup_verification_data.py --json
    python scripts/verification/cleanup_verification_data.py \
        --apply --manifest-id wiqa_20260830_epic \
        --actor-user-id <admin-uuid> \
        --preview-digest <digest> \
        --confirm "DELETE VERIFIED TEST DATA"

The one-time legacy manifests live in ``scripts/verification/manifests``.
Do not add a permanent title/name/age selector here; provenance is the only
deletion authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent / "manifests" / "wiqa_20260830_epic.json"
)
CONFIRMATION = "DELETE VERIFIED TEST DATA"
MAX_MANIFEST_BYTES = 1_048_576

# ``python scripts/verification/...`` places the script directory, not the
# repository root, at ``sys.path[0]``.  Make the package import explicit while
# keeping the script runnable from any working directory.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def validate_manifest_identity(
    manifest: Mapping[str, Any],
    *,
    expected_manifest_id: str | None = None,
) -> str:
    """Validate the non-negotiable, machine-readable manifest envelope.

    The service performs the authoritative database-side checks.  These
    inexpensive checks prevent an operator typo or an accidentally malformed
    local file from becoming an ambiguous cleanup request.  Notably, no
    human-readable title, status, timestamp, or regex is accepted as a
    selector.
    """

    schema_version = manifest.get("schema_version")
    if schema_version != 1:
        raise ValueError("unsupported cleanup manifest schema_version")

    manifest_id = manifest.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise ValueError("cleanup manifest requires a non-empty manifest_id")
    manifest_id = manifest_id.strip()
    if expected_manifest_id is not None and manifest_id != expected_manifest_id:
        raise ValueError(
            f"manifest id mismatch: file has {manifest_id!r}, "
            f"not {expected_manifest_id!r}"
        )

    run_id = manifest.get("run_id", manifest.get("verification_run_id"))
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("cleanup manifest requires a stable run_id")
    if manifest.get("disposable") is not True:
        raise ValueError("cleanup manifest is not explicitly disposable")
    if str(manifest.get("classification", "")).upper() != "A":
        raise ValueError("only category-A manifests may be executed")

    selection = manifest.get("selection")
    if not isinstance(selection, Mapping):
        raise ValueError("cleanup manifest requires an explicit selection object")
    for key in ("projects", "tasks", "users"):
        ids = selection.get(key)
        if not isinstance(ids, list) or any(
            not isinstance(entity_id, str) or not entity_id.strip() for entity_id in ids
        ):
            raise ValueError(f"selection.{key} must be a list of explicit ids")
    if not selection.get("projects") and not selection.get("tasks") and not selection.get("users"):
        raise ValueError("cleanup manifest selection cannot be empty")

    # Reject accidental broad selectors even if a future manifest loader were
    # to ignore unknown keys.  Explicit IDs above are the only accepted form.
    forbidden_keys = {
        "where",
        "query",
        "regex",
        "name_pattern",
        "title_pattern",
        "older_than",
        "inactive",
        "status",
        "all",
    }
    if forbidden_keys.intersection(manifest):
        raise ValueError("name/status/age/broad selectors are not allowed")

    return manifest_id


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or execute an explicit verification cleanup manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="path to a bounded JSON manifest (default: WIQA legacy manifest)",
    )
    parser.add_argument(
        "--manifest-id",
        help="exact manifest id; required for --apply (also checked when supplied for preview)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute canonical cleanup (preview is the default)",
    )
    parser.add_argument(
        "--actor-user-id",
        help="admin UUID acting on the cleanup; required for --apply",
    )
    parser.add_argument(
        "--preview-digest",
        help="digest returned by a fresh preview; required for --apply",
    )
    parser.add_argument(
        "--confirm",
        help=f'exact destructive confirmation phrase: {CONFIRMATION!r}',
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the service result as JSON instead of a human summary",
    )
    return parser.parse_args(argv)


def _load_manifest(path: Path) -> tuple[Mapping[str, Any], Any]:
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the bounded size limit")

    # Keep manifest parsing and digest normalization in the service.  Reading
    # this small file again only to validate its human-facing manifest key
    # happens before a DB session is created; no path is ever sent over an API.
    from src.services.verification_cleanup import load_cleanup_manifest

    loaded = load_cleanup_manifest(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("manifest root must be an object")
    return raw, loaded


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    if isinstance(value, UUID):
        return str(value)
    return value


def _validate_apply_args(args: argparse.Namespace, manifest_id: str) -> None:
    if not args.apply:
        return
    if args.manifest_id != manifest_id:
        raise ValueError(
            "--apply requires --manifest-id matching the manifest exactly"
        )
    if not args.actor_user_id:
        raise ValueError("--apply requires --actor-user-id")
    try:
        UUID(args.actor_user_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("--actor-user-id must be a UUID") from exc
    if not args.preview_digest or not args.preview_digest.strip():
        raise ValueError("--apply requires --preview-digest from a fresh preview")
    if args.confirm != CONFIRMATION:
        raise ValueError(f"--confirm must exactly equal {CONFIRMATION!r}")


async def _run(args: argparse.Namespace) -> Any:
    # Imports that construct engines are intentionally delayed until after
    # argument and manifest validation.  ``--help`` and malformed invocations
    # therefore never touch the runtime database.
    raw_manifest, frozen_manifest = _load_manifest(args.manifest)
    manifest_id = validate_manifest_identity(
        raw_manifest,
        expected_manifest_id=args.manifest_id if args.manifest_id else None,
    )
    _validate_apply_args(args, manifest_id)

    from src.memory.database import DatabaseManager
    from src.services.verification_cleanup import VerificationCleanupService

    database = DatabaseManager()
    coordinator = VerificationCleanupService()
    run_id = str(frozen_manifest.run_id)
    actor_user_id = args.actor_user_id if args.apply else None

    try:
        async with database.SessionLocal() as session:
            if not args.apply:
                return await coordinator.preview(
                    session,
                    run_id=run_id,
                    manifest=frozen_manifest,
                    actor_user_id=actor_user_id,
                )

            # ``execute`` performs the authoritative preview/digest check and
            # TOCTOU re-read itself.  Calling it directly also preserves the
            # service's idempotent retry path after a completed purge (where a
            # second standalone preview would correctly report target_missing).
            return await coordinator.execute(
                session,
                run_id=run_id,
                manifest=frozen_manifest,
                actor_user_id=args.actor_user_id,
                confirmation_digest=args.preview_digest.strip(),
                dry_run=False,
            )
    finally:
        await database.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("cleanup interrupted; no completion is reported", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - operator CLI needs concise failure
        if args.json:
            print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        else:
            print(f"cleanup failed: {exc}", file=sys.stderr)
        return 2

    payload = _jsonable(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        mode = "APPLIED" if args.apply else "PREVIEW"
        print(f"[{mode}] verification cleanup manifest: {args.manifest}")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by operator
    raise SystemExit(main())
