"""Readiness checks for platform disclosures; never an authority grant."""
from collections.abc import Mapping
from typing import Any


def product_rights_blockers(payload: Any) -> list[str]:
    """Honor explicit product-use checks without interpreting license text.

    A passed checklist is necessary, not a rights grant. Original-asset and
    generated-output conditions remain separate caller-authored checks; no
    successful generation, generic assessment or note overrides an unknown.
    """

    if not isinstance(payload, Mapping):
        return []
    kind = payload.get("type")
    if not isinstance(kind, str) or kind not in {"dlsite_release", "fanza_release"}:
        return []
    checks = payload.get("rights_checklist")
    if checks is None or checks == []:
        return ["product_rights_checklist_missing"]
    if not isinstance(checks, list) or len(checks) > 50:
        return ["product_rights_checklist_invalid"]
    statuses = set()
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {"code", "status", "note"}:
            return ["product_rights_checklist_invalid"]
        code, status, note = check["code"], check["status"], check["note"]
        if (
            not isinstance(code, str) or not code.strip() or len(code) > 120
            or not isinstance(status, str) or status not in {"passed", "failed", "not_run", "review_required"}
            or (note is not None and (not isinstance(note, str) or len(note) > 2000))
        ):
            return ["product_rights_checklist_invalid"]
        statuses.add(status)
    return [
        f"product_rights_checklist_{status}"
        for status in ("failed", "not_run", "review_required") if status in statuses
    ]


def platform_payload_blockers(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    from .media_publication_contract import x_publication_setting_blockers
    blockers = x_publication_setting_blockers(payload)
    if payload.get("type") == "fanza_release" and payload.get("ai_usage") not in {"none", "partial", "generated"}:
        blockers.append("fanza_ai_usage_unconfirmed")
    if payload.get("type") == "tiktok_video" and payload.get("music_rights") not in {"owned", "licensed", "no_music"}:
        blockers.append("tiktok_music_rights_unconfirmed")
    blockers.extend(product_rights_blockers(payload))
    return blockers
