"""One code-owned classification for reviewed publication body contracts."""
from collections.abc import Mapping, Sequence
from typing import Any


def publication_operation(action_type: Any, payload: Any) -> str:
    actions = {"media.release_product": "revenue", "media.delete_content": "delete", "media.update_content": "edit"}
    if action_type in actions:
        return actions[action_type]
    if not isinstance(payload, Mapping):
        return "unsupported"
    kind = payload.get("type")
    if payload.get("scheduled_at"):
        return "schedule"
    if kind in {"x_post", "x_thread"}:
        media = payload.get("media") or any(
            isinstance(post, Mapping) and post.get("media") for post in payload.get("posts", [])
        )
        return "image" if media else "text"  # image/video both require media.write
    if kind in {"youtube_video", "youtube_short", "instagram_reel", "tiktok_video"}:
        return "video"
    if kind in {"pixiv_work", "instagram_image", "instagram_carousel"}:
        return "image"
    if kind in {"patreon_post", "instagram_caption"}:
        return "text"
    return "unsupported"


def x_publication_setting_blockers(payload: Any) -> list[str]:
    """Stop unsupported reviewed settings rather than silently publishing without them."""
    if not isinstance(payload, Mapping) or payload.get("type") not in {"x_post", "x_thread"}:
        return []
    blockers = []
    if payload.get("sensitive_content") is True:
        blockers.append("x_api_sensitive_content_unsupported")
    posts = [payload] if payload.get("type") == "x_post" else payload.get("posts", [])
    if isinstance(posts, (str, bytes, bytearray)) or not isinstance(posts, Sequence):
        return blockers
    for post in posts:
        if not isinstance(post, Mapping):
            continue
        media = post.get("media", [])
        has_reference_alt = not isinstance(media, (str, bytes, bytearray)) and isinstance(media, Sequence) and any(
            isinstance(ref, Mapping) and ref.get("alt_text") not in (None, "") for ref in media
        )
        if post.get("alt_text") not in (None, "") or has_reference_alt:
            blockers.append("x_api_alt_text_unsupported")
            break
    return blockers


def official_write_blockers(platform: str, payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["provider_payload_unsupported"]
    if platform == "x":
        if payload.get("type") not in {"x_post", "x_thread"}:
            return ["provider_payload_unsupported"]
        if payload.get("scheduled_at"):
            return ["x_api_schedule_unsupported"]
        return x_publication_setting_blockers(payload)
    if platform == "youtube":
        if payload.get("type") not in {"youtube_video", "youtube_short"}:
            return ["provider_payload_unsupported"]
        if payload.get("captions"):
            return ["youtube_caption_upload_unsupported"]
    return []


def browser_publication_status(platform: str, readiness: dict | None = None) -> dict:
    blockers = ["browser_final_publication_command_unavailable", "browser_live_platform_acceptance_pending",
        "browser_provider_receipt_contract_missing"]
    url = None
    if platform == "x":
        blockers.insert(0, "x_non_api_automation_prohibited")
        url = "https://help.x.com/en/rules-and-policies/x-automation"
    elif platform == "youtube":
        blockers.insert(0, "youtube_prior_written_permission_unverified")
        url = "https://www.youtube.com/static?template=terms"
    preparation_blockers = ["browser_explicit_owner_device_tab_required"]
    result = {"publication_available": False, "automatic_fallback": False,
        "policy_source_url": url, "blockers": blockers,
        "preparation": {"available": False, "source_available": platform in {"x", "youtube"},
            "device_capability_required": True, "execution_mode": "manual", "publication_confirmation_allowed": False,
            "blockers": preparation_blockers,
            "verification_notes": ["browser_extension_distribution_acceptance_pending", "browser_live_platform_acceptance_pending"],
            "readiness_target": None,
            "reusable_contracts": ["owner_bound_pc_bridge_connection_epoch", "selected_existing_target",
                "strict_snapshot_references", "transport_unknown_no_replay"],
            "prepare_operation_id": "media_operations_prepare_browser_action",
            "get_operation_id": "media_operations_get_browser_preparation",
            "inspect_operation_id": "media_operations_inspect_browser_preparation",
            "supported_payloads": ["x_post_text", "x_post_single_image"] if platform == "x" else ["youtube_private_video"],
            "account_identifier": "explicit_x_handle" if platform == "x" else "explicit_youtube_channel_id",
            "initial_constraints": {"media_count": 1, "media_max_bytes": 25 * 1024 * 1024,
                "mime_types": ["image/jpeg", "image/png", "image/webp"], "sensitive_content": False, "alt_text": False,
                "reply": False, "thread": False, "schedule": False} if platform == "x" else {
                "media_count": 1, "media_max_bytes": 64 * 1024 * 1024, "mime_types": ["video/mp4", "video/webm"],
                "visibility": "private", "audience": "not_made_for_kids", "tags": [],
                "thumbnail": False, "captions": False, "disclosure": False, "schedule": False},
            "required_terminal_stage": "awaiting_final_confirmation",
            "required_saved_outcomes": ["blocked", "awaiting_final_confirmation", "uncertain"]}}

    if platform not in {"x", "youtube"}:
        result["preparation"].update(supported_payloads=[], account_identifier=None, initial_constraints={})
    elif readiness is not None and readiness.get("platform") == platform:
        result["preparation"].update({key: readiness[key] for key in ("available", "blockers", "readiness_target")})
    return result
