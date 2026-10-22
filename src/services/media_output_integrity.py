"""Live integrity checks for the generated-output manifest pinned by WS5.

This checks ledger identity, scope and fixed digests, not file readability or
product quality. It grants no QA, rights, provider or publication authority.
"""
from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.models.media_operations_generation import GenerationOutput


async def generation_output_blockers(session: Any, revision: Any) -> list[str]:
    """Reread every pinned output without relying on the ORM identity cache.

    A valid text/manual-material variant can have no generated-output refs.
    Explicit refs and payload refs are merged when a revision is created, so
    the persisted total is not restricted to the input's 20 explicit refs.
    """

    refs = revision.generation_output_refs_json
    if not isinstance(refs, list):
        return ["generation_output_manifest_invalid"]
    expected: dict[UUID, str] = {}
    for index, ref in enumerate(refs, 1):
        if not isinstance(ref, Mapping) or set(ref) != {"ordinal", "generation_output_id", "sha256"}:
            return ["generation_output_manifest_invalid"]
        if type(ref["ordinal"]) is not int or ref["ordinal"] != index:
            return ["generation_output_manifest_invalid"]
        try:
            output_id = UUID(str(ref["generation_output_id"]))
        except (ValueError, TypeError, AttributeError):
            return ["generation_output_manifest_invalid"]
        digest = ref["sha256"]
        if output_id in expected or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return ["generation_output_manifest_invalid"]
        expected[output_id] = digest

    # QA must cover every asset referenced by the actual typed write body,
    # including per-thread media and YouTube thumbnail. Extra explicitly
    # reviewed source assets are valid, but an unpinned write asset is not.
    from .media_operations_content_service import _normalize_payload
    try:
        _, payload_refs = _normalize_payload(revision.payload_json, revision.platform)
    except ValueError:
        return ["generation_output_payload_invalid"]
    if any(UUID(value) not in expected for value in payload_refs):
        return ["generation_output_payload_unpinned"]
    if not expected:
        return []

    # Select scalar columns instead of ORM objects: an already-loaded output
    # must not hide a committed deletion, scope change or digest replacement.
    result = session.execute(
        select(GenerationOutput.id, GenerationOutput.owner_user_id,
               GenerationOutput.project_id, GenerationOutput.sha256)
        .where(GenerationOutput.id.in_(expected))
    )
    if inspect.isawaitable(result):
        result = await result
    rows = result.all()
    blockers = []
    if len(rows) != len(expected):
        blockers.append("generation_output_missing")
    if any(row.owner_user_id != revision.owner_user_id or row.project_id != revision.project_id for row in rows):
        blockers.append("generation_output_scope_mismatch")
    if any(row.sha256 != expected[row.id] for row in rows):
        blockers.append("generation_output_hash_mismatch")
    return blockers
