"""Validate the immutable Operations source pin for a new delegation order.

Admission and authorization belong to the caller; this helper only verifies the
reference against the current opportunity in the already-authorized scope.
"""

import re
from uuid import UUID

from ..memory.models.operations import EngagementOpportunity
from .agent_delegation_service import DelegationError


def operations_source_matches(opportunity, expected_hash):
    """Match the stored canonical snapshot or the legacy text-only pointer."""
    from .operations_service import sha256_json, sha256_text

    snapshot = opportunity.source_snapshot_json
    if sha256_json(snapshot) == expected_hash:
        return True
    return (
        isinstance(snapshot, dict)
        and set(snapshot) == {"sha256"}
        and snapshot["sha256"] == expected_hash
        and isinstance(opportunity.source_text, str)
        and sha256_text(opportunity.source_text) == expected_hash
    )


async def validate_operations_source_ref(session, *, owner_user_id, project_id, source_ref):
    ref = source_ref
    if ref is None:
        return None
    if not isinstance(ref, dict) or set(ref) != {"opportunity_id", "source_snapshot_hash"}:
        raise DelegationError("delegation_operations_source_invalid")
    try:
        if not isinstance(ref["opportunity_id"], (str, UUID)):
            raise ValueError
        opportunity_id = UUID(str(ref["opportunity_id"]))
        owner_id = UUID(str(owner_user_id))
        project_uuid = UUID(str(project_id))
    except (ValueError, TypeError, AttributeError):
        raise DelegationError("delegation_operations_source_invalid") from None
    digest = ref["source_snapshot_hash"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise DelegationError("delegation_operations_source_invalid")
    opportunity = await session.get(EngagementOpportunity, opportunity_id, populate_existing=True)
    if opportunity is None or opportunity.owner_user_id != owner_id or opportunity.project_id != project_uuid:
        raise DelegationError("delegation_operations_source_scope_mismatch", 403)
    if opportunity.source_snapshot_hash != digest.lower():
        raise DelegationError("delegation_operations_source_changed", 409)
    return {"opportunity_id": str(opportunity.id), "source_snapshot_hash": opportunity.source_snapshot_hash}
