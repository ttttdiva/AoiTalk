"""Prepare an exact human-approved draft on an owner-selected product Bridge.

No command in this service can click Post, Publish, Save, or schedule submission.
Preparation evidence is deliberately separate from external publication receipts.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from pc_bridge.edge import EdgeBridgeError
from pc_bridge.browser_commands import preparation_backend

from ..memory.models import (
    ExternalAction, MediaBrowserPreparation, PlatformAccount, PlatformAccountRevision,
    ContentVariantRevision, MediaAssetRevision, PersonaRevision, Persona, ExternalConnection,
)
from ..security.media_credential_crypto import (
    decrypt_media_credential, encrypt_media_credential, MediaCredentialCryptoError, MediaCredentialKeyUnavailable,
    probe_media_credential_key_readonly,
)
from .operations_service import (
    OperationsAuthorizationError, OperationsConflictError, OperationsInvalidTransitionError,
    OperationsValidationError, OperationsError, sha256_json,
)
from .pc_bridge_service import RemoteControlBridge, get_pc_bridge_hub
from .media_bridge_asset_transfer import MediaBridgeAssetTransfer
from .pc_bridge_store import PcBridgeError

LEASE_SECONDS = 180
_HANDLE = re.compile(r"@[A-Za-z0-9_]{1,15}\Z")
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_CLOSED_UI_ERRORS = {
    "media_prepare_new_document_required": "browser_preparation_new_document_required",
    "media_prepare_legacy_revision_unverified": "browser_preparation_legacy_revision_unverified",
}


async def _resolve_bridge(owner: str, device: str):
    hub = get_pc_bridge_hub()
    selected = await hub.store.resolve(owner, device)
    return RemoteControlBridge(owner, selected, hub)


def browser_context(value):
    """Opaque profile/port identifiers; never a cookie, account or credential."""
    if not isinstance(value, dict) or set(value) != {"context_id", "context_epoch"}:
        raise OperationsInvalidTransitionError("browser_prepare_context_unverified")
    try:
        if any(not isinstance(v, str) or str(UUID(v)) != v or UUID(v).int == 0 for v in value.values()):
            raise ValueError()
    except ValueError:
        raise OperationsInvalidTransitionError("browser_prepare_context_unverified") from None
    return dict(value)


def verified_profile_binding(bridge, capabilities, context, *, platform, account_id, epoch):
    """Legacy extension pins or an owned-profile scope; never a backend label."""
    if getattr(bridge, "connection_epoch", None) != epoch:
        raise OperationsConflictError("browser_prepare_connection_changed")
    value = capabilities.get("profile_binding")
    if value is None and preparation_backend(bridge) == "extension":
        return None
    keys = {"version", "service", "owner_user_id", "platform_account_id", "profile_id", "connection_epoch"}
    if (account_id is None or not isinstance(epoch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", epoch)
            or not isinstance(value, dict) or set(value) != keys
            or type(value["version"]) is not int or value["version"] != 1
            or value["service"] != platform or value["owner_user_id"] != getattr(bridge, "owner", None)
            or value["platform_account_id"] != str(account_id) or value["profile_id"] != context["context_id"]
            or value["connection_epoch"] != epoch):
        raise OperationsInvalidTransitionError("browser_prepare_profile_binding_unverified")
    return dict(value)


class PinnedMediaBridge:
    """Every media command targets one profile, owner/account and live epoch."""
    def __init__(self, bridge, context, *, platform=None, account_id=None):
        self.bridge = bridge
        self.context = browser_context(context)
        self.platform, self.account_id = platform, account_id
        self.epoch = getattr(bridge, "connection_epoch", None)

    def __getattr__(self, name):
        return getattr(self.bridge, name)

    async def request(self, command, **params):
        if command.startswith("media_prepare_") and command not in {"media_prepare_capabilities", "media_prepare_context"}:
            if "expected_browser_context" in params:
                raise OperationsValidationError("browser_prepare_context_override_forbidden")
            if preparation_backend(self.bridge) != "extension":
                current = await MediaBrowserPreparationService._require_capabilities(self.bridge, self.platform,
                    has_asset="asset_" in command, account_id=self.account_id)
                if current != self.context:
                    raise OperationsConflictError("browser_prepare_bound_context_changed")
                if getattr(self.bridge, "connection_epoch", None) != self.epoch:
                    raise OperationsConflictError("browser_prepare_connection_changed")
            params["expected_browser_context"] = dict(self.context)
        return await self.bridge.request(command, **params)


class MediaBrowserPreparationService:
    def __init__(self, operations: Any, *, bridge_factory=None, transfer_factory=None):
        self.operations = operations
        self.bridge_factory = bridge_factory or _resolve_bridge
        self.transfer_factory = transfer_factory or MediaBridgeAssetTransfer

    async def _bridge(self, owner, device):
        try:
            return await self.bridge_factory(owner, device)
        except (PcBridgeError, TimeoutError):
            raise OperationsInvalidTransitionError("browser_prepare_device_unavailable") from None

    @staticmethod
    def _epoch(bridge):
        try:
            return bridge.connection_epoch
        except PcBridgeError:
            raise OperationsConflictError("browser_prepare_connection_changed") from None

    @staticmethod
    def _bound_context(connection, context):
        from .media_bridge_connection import manual_bridge
        if manual_bridge(connection) and connection.metadata_json.get("browser_context") != context:
            raise OperationsConflictError("browser_prepare_bound_context_changed")

    async def _current_binding(self, session, account, owner, *, for_update=False):
        """入口照会と実入力が共有する、現在のDB結合・停止条件。"""
        await session.refresh(account, with_for_update=for_update)
        revision = await session.scalar(select(PlatformAccountRevision)
            .where(PlatformAccountRevision.platform_account_id == account.id)
            .order_by(PlatformAccountRevision.version.desc(), PlatformAccountRevision.id.desc())
            .limit(1).execution_options(populate_existing=True))

        async def current(model, identifier):
            if identifier is None:
                return None
            statement = select(model).where(model.id == identifier).execution_options(populate_existing=True)
            if for_update:
                statement = statement.with_for_update()
            return await session.scalar(statement)

        persona = await current(Persona, account.persona_id)
        connection = await current(ExternalConnection, account.connection_id)
        scope = (owner, account.project_id)
        if (account.owner_user_id != owner or account.status != "active" or revision is None or persona is None or connection is None
                or any((row.owner_user_id, row.project_id) != scope for row in (revision, persona, connection))
                or persona.state in {"paused", "archived"}
                or connection.provider_key != account.platform or connection.remote_account_ref != account.account_ref
                or connection.auth_status in {"revoked", "disabled", "invalid", "disconnected", "expired", "error"}
                or revision.credential_status == "invalid"
                or revision.publish_capability == "unsupported" or revision.media_capability == "unsupported"):
            return None
        from .media_bridge_connection import manual_bridge
        if manual_bridge(connection) and (connection.credential_ref is not None or connection.auth_status != "unknown"
                or connection.metadata_json.get("platform_account_id") != str(account.id)
                or connection.metadata_json.get("persona_id") != str(persona.id)
                or connection.metadata_json.get("binding_revision_id") != str(revision.id)):
            return None
        return revision, persona, connection

    async def _require_active_action_binding(self, session, action, *, for_update=False):
        account = await self.operations._get_or_404(session, PlatformAccount, action.platform_account_id, "platform_account_id")
        rows = await self._current_binding(session, account, action.owner_user_id, for_update=for_update)
        persona_revision = await self.operations._get_or_404(session, PersonaRevision, action.persona_revision_id, "persona_revision_id")
        if (rows is None or account.project_id != action.project_id or account.platform != action.platform
                or rows[1].id != persona_revision.persona_id or rows[2].id != action.connection_id):
            raise OperationsInvalidTransitionError("browser_prepare_account_binding_unavailable")
        from .media_bridge_connection import manual_bridge
        if manual_bridge(rows[2]) and rows[0].id != action.platform_account_revision_id:
            raise OperationsConflictError("browser_prepare_bound_account_revision_changed")
        return rows

    async def readiness(self, session, actor, *, platform_account_id, device_id=None, tab_id=None):
        """選択済み対象の準備入口を照会する。承認・入力・鍵作成・台帳書込は行わない。"""
        owner = await self.operations._assert_human(actor)
        account = await self.operations._get_or_404(session, PlatformAccount, platform_account_id, "platform_account_id")
        await self.operations._assert_entity_access(session, actor, account, permission="write")
        if account.owner_user_id != owner:
            raise OperationsAuthorizationError("browser readiness requires the account owner's Bridge")
        await session.refresh(account)
        platform = account.platform
        result = {"platform": platform, "available": False, "blockers": [], "readiness_target": None}

        def blocked(code):
            result["blockers"] = [code]
            return result

        async def binding():
            rows = await self._current_binding(session, account, owner)
            await self.operations._assert_entity_access(session, actor, account, permission="write")
            if account.owner_user_id != owner:
                raise OperationsAuthorizationError("browser readiness account owner changed")
            if rows is None:
                return None
            revision, persona, connection = rows
            if revision.content_hash != self.operations._media_account_revision_content_hash(revision):
                return None
            return (str(account.id), str(revision.id), str(owner), str(account.project_id) if account.project_id else None,
                    str(persona.id), str(connection.id), connection.version, account.platform, account.account_ref)

        pinned = await binding()
        if pinned is None:
            return blocked("browser_prepare_account_binding_unavailable")
        connection = await session.get(ExternalConnection, UUID(pinned[5]))
        from .media_bridge_connection import manual_bridge
        if manual_bridge(connection) and (connection.metadata_json.get("device_id") != device_id
                or connection.metadata_json.get("tab_id") != tab_id):
            return blocked("browser_prepare_bound_device_tab_mismatch")
        platform = pinned[7]
        result["platform"] = platform
        result["readiness_target"] = {"owner_user_id": pinned[2], "project_id": pinned[3], "persona_id": pinned[4],
            "platform_account_id": pinned[0], "platform_account_revision_id": pinned[1], "platform": platform,
            "device_id": device_id, "tab_id": tab_id, "checked_at": datetime.utcnow().isoformat() + "Z"}
        if platform not in {"x", "youtube"}:
            return blocked("browser_prepare_platform_unsupported")
        ref = pinned[8]
        if not isinstance(ref, str) or not (_HANDLE if platform == "x" else _CHANNEL).fullmatch(ref):
            return blocked("browser_account_identifier_unsupported")
        ref = ref.lower() if platform == "x" else ref
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 120 or type(tab_id) is not int or tab_id < 1:
            return blocked("browser_explicit_owner_device_tab_required")
        lock = None
        acquired = False
        try:
            bridge = await self._bridge(str(owner), device_id)
            if getattr(bridge, "owner", None) != str(owner) or getattr(bridge, "device_id", None) != device_id:
                return blocked("browser_prepare_bridge_capability_unavailable")
            lock = getattr(bridge, "operation_lock", None)
            if lock is None or not lock.acquire(blocking=False):
                return blocked("browser_prepare_device_busy")
            acquired = True
            epoch = self._epoch(bridge)
            # 広告する全 payload（X画像を含む）に必要な installed capability を確認する。
            context = await self._require_capabilities(bridge, platform, has_asset=True, account_id=account.id)
            self._bound_context(connection, context)
            bridge = PinnedMediaBridge(bridge, context, platform=platform, account_id=account.id)
            try:
                usable_key = probe_media_credential_key_readonly()
            except MediaCredentialCryptoError:
                usable_key = False
            if usable_key is not True:
                return blocked("browser_preparation_checkpoint_key_unavailable")
            response = await bridge.request("media_prepare_readiness", platform=platform, tab_id=tab_id, expected_account_ref=ref)
            required = {"platform", "account_ref", "account_ref_kind", "document_id", "helper_revision", "composer_ready",
                "account_verified", "available", "executed", "publication_confirmed", "outcome_unknown"}
            if (not isinstance(response, dict) or not required.issubset(response) or set(response) - required - {"error"}
                    or response.get("platform") != platform
                    or response.get("account_ref_kind") != ("x_handle" if platform == "x" else "youtube_channel_id")
                    or type(response.get("helper_revision")) is not int or response["helper_revision"] != (9 if platform == "x" else 4)
                    or not isinstance(response.get("account_ref"), str)
                    or not isinstance(response.get("document_id"), str) or len(response["document_id"]) > 128
                    or any(type(response.get(key)) is not bool for key in ("composer_ready", "account_verified", "available"))
                    or any(response.get(key) is not False for key in ("executed", "publication_confirmed", "outcome_unknown"))
                    or ("error" in response and (not isinstance(response["error"], str)
                        or not re.fullmatch(r"media_prepare_[a-z_]{1,90}", response["error"])))
                    or (response["account_verified"] and response["account_ref"] != ref)):
                return blocked("browser_prepare_readiness_unverified")
            if self._epoch(bridge) != epoch or await binding() != pinned or self._epoch(bridge) != epoch:
                return blocked("browser_prepare_readiness_binding_changed")
            if response["available"] is not True:
                error = response.get("error")
                return blocked(_CLOSED_UI_ERRORS.get(error, "browser_prepare_readiness_unverified"))
            if (response.get("error") is not None or not response["document_id"] or response["account_ref"] != ref
                    or response["composer_ready"] is not True or response["account_verified"] is not True):
                return blocked("browser_prepare_readiness_unverified")
            result["available"] = True
            return result
        except (PcBridgeError, EdgeBridgeError, TimeoutError):
            return blocked("browser_prepare_device_unavailable")
        except (OperationsConflictError, OperationsInvalidTransitionError) as exc:
            code = str(exc)
            allowed = {"browser_prepare_device_unavailable", "browser_prepare_connection_changed", "browser_prepare_bound_context_changed",
                "browser_prepare_extension_update_required", "browser_prepare_pc_asset_update_required"}
            return blocked(code if code in allowed else "browser_prepare_readiness_unverified")
        finally:
            if acquired:
                lock.release()

    async def _action(self, session, actor, action_id, *, write=False):
        if write:
            principal = await self.operations._assert_human(actor)
        action = await self.operations._get_or_404(session, ExternalAction, action_id, "action_id", for_update=write)
        if write:
            # Refresh after acquiring the lock: identity-map values may predate a
            # concurrent rejection/revision committed at a durable checkpoint.
            await session.refresh(action, with_for_update=True)
        await self.operations._assert_entity_access(session, actor, action, permission="write" if write else "read")
        if write and principal != action.owner_user_id:
            raise OperationsAuthorizationError("browser preparation requires the action owner's Bridge")
        if action.action_type != "media.publish_content" or action.execution_mode != "manual":
            raise OperationsInvalidTransitionError("browser preparation requires an explicitly approved manual media action")
        if write:
            await self.operations._assert_media_action_integrity(session, actor, action)
            if action.status != "approved" or await self.operations._current_valid_approval(session, action) is None:
                raise OperationsInvalidTransitionError("current human approval is required for draft preparation")
        return action

    @staticmethod
    async def _require_capabilities(bridge, platform, *, has_asset, account_id=None):
        epoch = getattr(bridge, "connection_epoch", None)
        try:
            capabilities = await bridge.request("media_prepare_capabilities")
        except (PcBridgeError, EdgeBridgeError, TimeoutError):
            raise OperationsInvalidTransitionError("browser_prepare_extension_update_required") from None
        if (not isinstance(capabilities, dict) or type(capabilities.get("version")) is not int or capabilities.get("version") != 2
                or not isinstance(capabilities.get("platforms"), list)
                or capabilities.get("final_confirmation") is not False or platform not in capabilities.get("platforms", [])
                or capabilities.get("browser_context_pin") is not True
                or (platform == "x" and capabilities.get("text") is not True)
                or (platform == "x" and has_asset and capabilities.get("x_image") is not True)
                or (platform == "youtube" and capabilities.get("youtube_private") is not True)):
            raise OperationsInvalidTransitionError("browser_prepare_extension_update_required")
        if has_asset and "media_asset" not in getattr(getattr(bridge, "_connection", None), "capabilities", []):
            raise OperationsInvalidTransitionError("browser_prepare_pc_asset_update_required")
        context = browser_context(capabilities.get("browser_context"))
        verified_profile_binding(bridge, capabilities, context, platform=platform, account_id=account_id, epoch=epoch)
        return context

    @staticmethod
    def _binding(row):
        return {key: str(getattr(row, key)) for key in (
            "id", "action_id", "owner_user_id", "project_id", "action_version", "action_state_version",
            "payload_hash", "platform_account_id", "platform_account_revision_id", "device_id", "tab_id",
            "command_hash", "text_sha256", "platform",
        )}

    def _encode(self, row, state):
        try:
            return encrypt_media_credential({"binding": self._binding(row), "state": state},
                credential_id=f"browser-preparation:{row.id}", platform_account_id=row.platform_account_id).ciphertext
        except MediaCredentialCryptoError:
            raise OperationsInvalidTransitionError("browser_preparation_checkpoint_key_unavailable") from None

    def _decode(self, row):
        try:
            value = decrypt_media_credential(row.checkpoint_ciphertext,
                credential_id=f"browser-preparation:{row.id}", platform_account_id=row.platform_account_id)
        except MediaCredentialKeyUnavailable:
            raise OperationsInvalidTransitionError("browser_preparation_checkpoint_key_unavailable") from None
        except MediaCredentialCryptoError:
            raise OperationsConflictError("browser_preparation_checkpoint_invalid") from None
        binding = self._binding(row)
        legacy = {key: val for key, val in binding.items() if key != "platform"}
        if (not isinstance(value, dict) or not isinstance(value.get("state"), dict)
                or (value.get("binding") != binding and not (row.platform == "x"
                    and value.get("binding") == legacy and value["state"].get("asset_manifest") is None))):
            raise OperationsConflictError("browser preparation checkpoint binding changed")
        return value["state"]

    @staticmethod
    def _identity(action, account, revision):
        if account.status != "active" or account.persona_id is None:
            raise OperationsInvalidTransitionError("browser account is inactive or not bound to the reviewed persona")
        pinned = (action.payload_json or {}).get("connection", {}).get("remote_account_ref")
        if revision.id != action.platform_account_revision_id or revision.platform_account_id != account.id or pinned != account.account_ref:
            raise OperationsConflictError("browser account reference changed since approval")
        ref = pinned
        if action.platform == "youtube":
            if not isinstance(ref, str) or not _CHANNEL.fullmatch(ref):
                raise OperationsInvalidTransitionError("browser_account_identifier_unsupported: explicitly configure the own YouTube UC channel ID")
            return ref
        # The current fixed DOM contract proves a handle, never an inferred numeric ID.
        if not isinstance(ref, str) or not _HANDLE.fullmatch(ref):
            raise OperationsInvalidTransitionError("browser_account_identifier_unsupported: explicitly configure the own X handle; numeric IDs cannot be inferred")
        return ref.lower()

    @staticmethod
    def _text(action):
        from .media_provider_execution import MediaProviderExecutionRouter
        raw = action.payload_json or {}
        payload = raw.get("payload", {})
        if (action.platform != "x" or payload.get("type") != "x_post" or len(payload.get("media") or []) > 1
                or payload.get("reply_to") or payload.get("scheduled_at") or raw.get("schedule")):
            raise OperationsInvalidTransitionError("browser_prepare_payload_unsupported: X prepares text and at most one image")
        text = MediaProviderExecutionRouter._x_text(payload)
        if len(text.encode("utf-8")) > 16000:
            raise OperationsValidationError("browser preparation text exceeds the bounded input contract")
        return text

    @classmethod
    def _content(cls, action):
        if action.platform == "x":
            return cls._text(action)
        raw, payload = action.payload_json or {}, (action.payload_json or {}).get("payload", {})
        if (action.platform != "youtube" or payload.get("type") != "youtube_video"
                or payload.get("visibility") != "private" or payload.get("audience") != "not_made_for_kids"
                or payload.get("tags") or len(payload.get("media_asset") or []) != 1
                or raw.get("schedule") or any(payload.get(key) for key in ("thumbnail", "captions", "scheduled_at", "disclosure"))):
            raise OperationsInvalidTransitionError("browser_prepare_payload_unsupported: YouTube requires a private video, explicit not_made_for_kids, empty tags and no additional settings")
        metadata = {"title": payload.get("title"), "description": payload.get("description"), "tags": [],
            "visibility": "private", "audience": "not_made_for_kids"}
        if (not isinstance(metadata["title"], str) or not 1 <= len(metadata["title"]) <= 100
                or not isinstance(metadata["description"], str) or len(metadata["description"]) > 5000
                or re.search(r"[<>\x00-\x08\x0b-\x1f\x7f]", metadata["title"] + metadata["description"])):
            raise OperationsValidationError("browser preparation YouTube metadata exceeds the bounded contract")
        return metadata

    @staticmethod
    def _content_hash(content):
        return hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) else sha256_json(content)

    async def _manifest(self, session, action):
        payload = (action.payload_json or {}).get("payload", {})
        ids = payload.get("media" if action.platform == "x" else "media_asset") or []
        if not ids:
            return None
        if len(ids) != 1:
            raise OperationsInvalidTransitionError("browser_prepare_asset_unsupported: exactly one managed asset is supported")
        revision = await self.operations._get_or_404(session, ContentVariantRevision, action.content_variant_revision_id, "content_variant_revision_id")
        await session.refresh(revision)
        pinned = next((ref for ref in revision.generation_output_refs_json or []
            if ref.get("generation_output_id") == ids[0]), None)
        if not pinned or not isinstance(pinned.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", pinned["sha256"]):
            raise OperationsConflictError("browser preparation asset is not pinned to the reviewed output")
        asset = await session.scalar(select(MediaAssetRevision).where(MediaAssetRevision.generation_output_id == UUID(ids[0]))
            .execution_options(populate_existing=True))
        persona = await self.operations._get_or_404(session, PersonaRevision, action.persona_revision_id, "persona_revision_id")
        if (asset is None or (asset.owner_user_id, asset.project_id, asset.persona_id) !=
                (action.owner_user_id, action.project_id, persona.persona_id) or asset.sha256 != pinned["sha256"]):
            raise OperationsConflictError("browser preparation requires an immutable managed asset of the reviewed owner/project/persona")
        mimes, maximum = ({"image/png", "image/jpeg", "image/webp"}, 25 * 1024 * 1024) if action.platform == "x" else ({"video/mp4", "video/webm"}, 64 * 1024 * 1024)
        if asset.mime_type not in mimes or not 0 < asset.size_bytes <= maximum:
            raise OperationsInvalidTransitionError("browser_prepare_asset_unsupported: asset format/size exceeds the initial DOM capability")
        return {"generation_output_id": ids[0], "sha256": pinned["sha256"], "size_bytes": asset.size_bytes, "mime_type": asset.mime_type}

    async def get(self, session, actor, action_id):
        action = await self._action(session, actor, action_id)
        if action.browser_preparation_id is None:
            return {"preparation": None, "publication_confirmed": False}
        row = await self.operations._get_or_404(session, MediaBrowserPreparation, action.browser_preparation_id, "preparation_id")
        if row.action_id != action.id or row.owner_user_id != action.owner_user_id or row.project_id != action.project_id:
            raise OperationsConflictError("browser preparation scope changed")
        return {"preparation": await self._safe(session, actor, action, row), "publication_confirmed": False}

    async def _safe(self, session, actor, action, row):
        result = row.to_safe_dict()
        matches = (action.browser_preparation_id == row.id and action.version == row.action_state_version
            and action.action_version == row.action_version and action.payload_hash == row.payload_hash
            and action.platform == row.platform
            and action.platform_account_id == row.platform_account_id
            and action.platform_account_revision_id == row.platform_account_revision_id)
        valid = False
        if matches and action.status == "approved":
            try:
                account = await self.operations._get_or_404(session, PlatformAccount, row.platform_account_id, "platform_account_id")
                await session.refresh(account)
                await self.operations._assert_media_action_integrity(session, actor, action)
                await self._require_active_action_binding(session, action)
                valid = await self.operations._current_valid_approval(session, action) is not None
            except OperationsError:
                valid = False
        # Evidence describes the pinned historical draft; authority can be revoked
        # independently and must never be inferred from an awaiting status alone.
        result.update(current_action_matches=matches, current_approval_valid=valid)
        return result

    async def prepare(self, session, actor, action_id, *, expected_version, action_version, payload_hash,
                      device_id, tab_id, idempotency_key):
        action = await self._action(session, actor, action_id, write=True)
        if action.action_version != action_version or action.payload_hash != payload_hash:
            raise OperationsConflictError("browser preparation must match the previewed action revision/hash")
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
            raise OperationsValidationError("device_id is invalid")
        if type(tab_id) is not int or tab_id <= 0:
            raise OperationsValidationError("tab_id must be the explicitly selected existing tab")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
            raise OperationsValidationError("idempotency_key is required")
        text = self._content(action)
        text_hash = self._content_hash(text)
        manifest = await self._manifest(session, action)
        command = {"action_id": str(action.id), "action_version": action_version,
            "payload_hash": payload_hash, "device_id": device_id, "tab_id": tab_id, "text_sha256": text_hash}
        if manifest is not None:
            command["asset_manifest"] = manifest
        command_hash = sha256_json(command)
        if action.browser_preparation_id is not None:
            row = await self.operations._get_or_404(session, MediaBrowserPreparation, action.browser_preparation_id, "preparation_id")
            if (row.action_id != action.id or row.action_version != action_version or row.payload_hash != payload_hash
                    or row.command_hash != command_hash or row.idempotency_key != idempotency_key):
                raise OperationsConflictError("the approved action already has another browser preparation; never replay it")
            return await self._safe(session, actor, action, row)
        await self.operations._check_expected_version(action, expected_version)
        await self._require_active_action_binding(session, action, for_update=True)
        connection = await self.operations._get_or_404(session, ExternalConnection, action.connection_id, "connection_id")
        from .media_bridge_connection import manual_bridge
        if manual_bridge(connection) and (connection.metadata_json.get("device_id") != device_id
                or connection.metadata_json.get("tab_id") != tab_id):
            raise OperationsConflictError("browser_prepare_bound_device_tab_mismatch")
        account = await self.operations._get_or_404(session, PlatformAccount, action.platform_account_id, "platform_account_id")
        revision = await self.operations._get_or_404(session, PlatformAccountRevision, action.platform_account_revision_id, "platform_account_revision_id")
        expected_ref = self._identity(action, account, revision)
        bridge = await self._bridge(str(action.owner_user_id), device_id)
        if getattr(bridge, "owner", None) != str(action.owner_user_id) or getattr(bridge, "device_id", None) != device_id:
            raise OperationsInvalidTransitionError("browser_prepare_bridge_capability_unavailable")
        lock = getattr(bridge, "operation_lock", None)
        if lock is None or not lock.acquire(blocking=False):
            raise OperationsConflictError("browser preparation device is busy")
        row = None
        try:
            context = await self._require_capabilities(bridge, action.platform, has_asset=bool(manifest), account_id=account.id)
            self._bound_context(connection, context)
            bridge = PinnedMediaBridge(bridge, context, platform=action.platform, account_id=account.id)
            # capability照会のawait中に停止されたPersonaをclaimしない。
            await self._require_active_action_binding(session, action, for_update=True)
            row = MediaBrowserPreparation(id=uuid4(), action_id=action.id, owner_user_id=action.owner_user_id,
                project_id=action.project_id, action_version=action.action_version, action_state_version=action.version + 1,
                payload_hash=action.payload_hash, platform_account_id=action.platform_account_id,
                platform_account_revision_id=action.platform_account_revision_id, platform=action.platform,
                device_id=device_id, tab_id=tab_id, idempotency_key=idempotency_key, command_hash=command_hash,
                text_sha256=text_hash, status="preparing", phase="claimed", version=1,
                lease_expires_at=datetime.utcnow() + timedelta(seconds=LEASE_SECONDS))
            state = {"connection_epoch": self._epoch(bridge), "browser_context": context, "expected_account_ref": expected_ref,
                     "document_id": None, "lease_token": uuid4().hex, "asset_manifest": manifest,
                     "asset_receipt": None}
            row.checkpoint_ciphertext = self._encode(row, state)
            # Claim before any DOM fill. PostgreSQL row lock plus CAS also fences SQLite races.
            claimed = await session.execute(update(ExternalAction).where(ExternalAction.id == action.id,
                ExternalAction.version == expected_version, ExternalAction.status == "approved",
                ExternalAction.browser_preparation_id.is_(None)).values(browser_preparation_id=row.id,
                    version=ExternalAction.version + 1).execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                await session.rollback()
                raise OperationsConflictError("browser preparation was claimed concurrently")
            session.add(row)
            await self.operations._event(session, actor=actor, entity_type="action", entity_id=action.id,
                event_type="media.browser_preparation.claimed", owner_user_id=action.owner_user_id,
                project_id=action.project_id, payload={"preparation_id": str(row.id), "action_version": row.action_version,
                    "payload_hash": row.payload_hash, "content_sha256": row.text_sha256,
                    "text_sha256": row.text_sha256 if row.platform == "x" else None,
                    "metadata_sha256": row.text_sha256 if row.platform == "youtube" else None})
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise OperationsConflictError("browser preparation was claimed concurrently") from None
            await session.refresh(action)
            return await self._run(session, actor, action, row, bridge, state, text, fill=True)
        finally:
            lock.release()

    async def inspect(self, session, actor, action_id, *, preparation_id, expected_version):
        action = await self._action(session, actor, action_id, write=True)
        if str(action.browser_preparation_id) != str(preparation_id):
            raise OperationsConflictError("browser preparation does not match the action")
        row = await self.operations._get_or_404(session, MediaBrowserPreparation, preparation_id, "preparation_id", for_update=True)
        if row.action_id != action.id or row.version != expected_version:
            raise OperationsConflictError("browser preparation version changed")
        if row.status == "preparing" and row.lease_expires_at and row.lease_expires_at > datetime.utcnow():
            raise OperationsConflictError("browser preparation is still leased")
        text = self._content(action)
        if self._content_hash(text) != row.text_sha256:
            raise OperationsConflictError("browser preparation text changed")
        state = self._decode(row)
        if await self._manifest(session, action) != state.get("asset_manifest"):
            raise OperationsConflictError("browser preparation asset binding changed")
        bridge = await self._bridge(str(action.owner_user_id), row.device_id)
        if bridge.owner != str(action.owner_user_id) or self._epoch(bridge) != state["connection_epoch"]:
            raise OperationsConflictError("browser preparation connection changed; new input is forbidden")
        lock = bridge.operation_lock
        if not lock.acquire(blocking=False):
            raise OperationsConflictError("browser preparation device is busy")
        try:
            context = await self._require_capabilities(bridge, row.platform, has_asset=bool(state.get("asset_manifest")),
                account_id=row.platform_account_id)
            if context != state.get("browser_context"):
                raise OperationsConflictError("browser_prepare_bound_context_changed")
            bridge = PinnedMediaBridge(bridge, context, platform=row.platform, account_id=row.platform_account_id)
            state["lease_token"] = uuid4().hex
            await self._save(session, actor, action, row, state, status="preparing", phase="inspect_only", reclaim=True)
            return await self._run(session, actor, action, row, bridge, state, text, fill=False)
        finally:
            lock.release()

    async def _check(self, session, actor, action, row, state, bridge, *, require_active=True):
        # Lock in the same action -> preparation order as claim/recovery. The
        # transaction keeps authority locked through the following DOM command.
        action = await self._action(session, actor, action.id, write=True)
        await session.refresh(row, with_for_update=True)
        if (action.browser_preparation_id != row.id or action.version != row.action_state_version
                or action.action_version != row.action_version or action.payload_hash != row.payload_hash
                or (action.owner_user_id, action.project_id, action.platform, action.platform_account_id, action.platform_account_revision_id)
                    != (row.owner_user_id, row.project_id, row.platform, row.platform_account_id, row.platform_account_revision_id)
                or row.status != "preparing" or row.lease_expires_at is None or row.lease_expires_at <= datetime.utcnow()
                or self._decode(row).get("lease_token") != state.get("lease_token")):
            raise OperationsConflictError("browser preparation authority or lease changed")
        if self._epoch(bridge) != state["connection_epoch"]:
            raise OperationsConflictError("browser preparation connection changed")
        account = await self.operations._get_or_404(session, PlatformAccount, row.platform_account_id, "platform_account_id")
        revision = await self.operations._get_or_404(session, PlatformAccountRevision, row.platform_account_revision_id, "platform_account_revision_id")
        await session.refresh(account, with_for_update=True)
        await session.refresh(revision)
        await self.operations._assert_media_action_integrity(session, actor, action)
        if require_active:
            # Persona停止と接続失効を現在値で再読し、次の入力までDB lockを保持する。
            await self._require_active_action_binding(session, action, for_update=True)
        if self._identity(action, account, revision) != state["expected_account_ref"]:
            raise OperationsConflictError("browser preparation account changed")
        if await self._manifest(session, action) != state.get("asset_manifest"):
            raise OperationsConflictError("browser preparation asset binding changed")

    async def _save(self, session, actor, action, row, state, *, status, phase, error_code=None, reclaim=False):
        old_version = row.version
        if not reclaim and self._decode(row).get("lease_token") != state.get("lease_token"):
            raise OperationsConflictError("browser preparation lease was replaced")
        values = {"version": old_version + 1, "status": status, "phase": phase, "error_code": error_code,
            "checkpoint_ciphertext": self._encode(row, state),
            "lease_expires_at": datetime.utcnow() + timedelta(seconds=LEASE_SECONDS) if status == "preparing" else None,
            "finished_at": None if status == "preparing" else datetime.utcnow()}
        condition = [MediaBrowserPreparation.id == row.id, MediaBrowserPreparation.version == old_version]
        if not reclaim:
            condition += [MediaBrowserPreparation.status == "preparing", MediaBrowserPreparation.lease_expires_at > datetime.utcnow()]
        result = await session.execute(update(MediaBrowserPreparation).where(*condition).values(**values)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await session.rollback()
            raise OperationsConflictError("browser preparation worker became stale")
        await self.operations._event(session, actor=actor, entity_type="action", entity_id=action.id,
            event_type=f"media.browser_preparation.{status}", owner_user_id=row.owner_user_id, project_id=row.project_id,
            payload={"preparation_id": str(row.id), "phase": phase, "content_sha256": row.text_sha256,
                     "text_sha256": row.text_sha256 if row.platform == "x" else None,
                     "metadata_sha256": row.text_sha256 if row.platform == "youtube" else None,
                     "publication_confirmed": False, "automatic_retry_allowed": False, "error_code": error_code})
        await session.commit()
        await session.refresh(row)

    @staticmethod
    def _response(result, *, ref, document=None, text_hash=None, prepared=False):
        if (not isinstance(result, dict) or result.get("platform") != "x" or result.get("account_ref_kind") != "x_handle"
                or result.get("account_ref") != ref or result.get("outcome_unknown") is not False or result.get("error")
                or result.get("composer_ready") is not True
                or not isinstance(result.get("document_id"), str) or not 1 <= len(result["document_id"]) <= 128
                or (document is not None and result["document_id"] != document)
                or (text_hash is not None and result.get("text_sha256") != text_hash)
                or (prepared and (result.get("prepared") is not True or result.get("final_confirmation_available") is not True))):
            raise OperationsConflictError("browser_preparation_ui_verification_failed")
        return result

    @staticmethod
    def _asset_response(result, row, state, *, inspected=False):
        if isinstance(result, dict) and isinstance(result.get("error"), str) and result["error"] in _CLOSED_UI_ERRORS:
            raise OperationsConflictError(_CLOSED_UI_ERRORS[result["error"]])
        receipt = state.get("asset_receipt")
        if not isinstance(receipt, dict):
            raise OperationsConflictError("browser_preparation_asset_evidence_missing")
        expected = {"transfer_id": receipt["transfer_id"], "scope_hash": row.command_hash,
            "document_id": state["document_id"], "account_ref": state["expected_account_ref"],
            "asset_sha256": receipt["sha256"], "offset": receipt["size_bytes"]}
        if (not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items())
                or any(result.get(key) is not True for key in ("accepted", "files_verified", "attachment_visible"))
                or result.get("outcome_unknown") is not False or result.get("error")
                or result.get("executed") is not (not inspected) or result.get("dispatched") is not (not inspected)):
            raise OperationsConflictError("browser_preparation_asset_verification_failed")
        return result

    async def _input_asset(self, session, actor, action, row, bridge, state):
        transfer = self.transfer_factory(bridge)
        manifest = state["asset_manifest"]
        if state.get("asset_receipt") or state.get("asset_transfer_unknown_id"):
            raise OperationsConflictError("browser preparation asset input must never be replayed")
        await self._check(session, actor, action, row, state, bridge)
        await self._save(session, actor, action, row, state, status="preparing", phase="asset_transfer_pending")
        await self._check(session, actor, action, row, state, bridge)
        state["input_may_have_run"] = True
        async with asyncio.timeout(120):
            try:
                receipt = await transfer.transfer(session, UUID(manifest["generation_output_id"]),
                    owner_user_id=action.owner_user_id, project_id=action.project_id,
                    expected_sha256=manifest["sha256"], scope_hash=row.command_hash)
            except BaseException as exc:
                state["asset_transfer_unknown_id"] = getattr(exc, "transfer_id", None)
                raise
        if (receipt.connection_epoch != state["connection_epoch"] or receipt.sha256 != manifest["sha256"]
                or receipt.size_bytes != manifest["size_bytes"] or receipt.mime_type != manifest["mime_type"]):
            raise OperationsConflictError("browser preparation asset transfer changed")
        state["asset_receipt"] = receipt.to_safe_dict()
        await self._save(session, actor, action, row, state, status="preparing", phase="asset_input_pending")
        await self._check(session, actor, action, row, state, bridge)
        pins = {"tab_id": row.tab_id, "run_id": row.id.hex, "expected_document_id": state["document_id"],
            "expected_account_ref": state["expected_account_ref"]}
        if action.platform == "x":
            result = await transfer.prepare_x_image(receipt, text_sha256=row.text_sha256, **pins)
        else:
            result = await transfer.prepare_youtube_video(receipt, metadata_sha256=row.text_sha256, **pins)
        self._asset_response(result, row, state)

    async def _inspect_asset(self, row, bridge, state):
        receipt = state.get("asset_receipt")
        if not isinstance(receipt, dict) or not state.get("document_id"):
            raise OperationsConflictError("browser_preparation_asset_evidence_missing")
        params = {"platform": row.platform, "tab_id": row.tab_id, "run_id": row.id.hex,
            "expected_document_id": state["document_id"], "expected_account_ref": state["expected_account_ref"],
            "scope_hash": row.command_hash, "transfer_id": receipt["transfer_id"],
            "asset_sha256": receipt["sha256"], "size_bytes": receipt["size_bytes"], "mime_type": receipt["mime_type"],
            "text_sha256" if row.platform == "x" else "metadata_sha256": row.text_sha256}
        command = "media_prepare_asset_inspect" if row.platform == "x" else "media_prepare_youtube_asset_inspect"
        self._asset_response(await bridge.request(command, **params), row, state, inspected=True)

    @staticmethod
    def _youtube_response(result, *, ref, document=None, metadata_hash=None, metadata=None, asset_hash=None, prepared=False):
        if isinstance(result, dict) and isinstance(result.get("error"), str) and result["error"] in _CLOSED_UI_ERRORS:
            raise OperationsConflictError(_CLOSED_UI_ERRORS[result["error"]])
        if (not isinstance(result, dict) or result.get("platform") != "youtube"
                or result.get("account_ref_kind") != "youtube_channel_id" or result.get("account_ref") != ref
                or result.get("outcome_unknown") is not False or result.get("error")
                or result.get("composer_ready") is not True
                or not isinstance(result.get("document_id"), str) or not 1 <= len(result["document_id"]) <= 128
                or (document is not None and result["document_id"] != document)
                or (metadata_hash is not None and result.get("metadata_sha256") != metadata_hash)
                or (metadata is not None and (result.get("title_sha256") != hashlib.sha256(metadata["title"].encode()).hexdigest()
                    or result.get("description_sha256") != hashlib.sha256(metadata["description"].encode()).hexdigest()))
                or (asset_hash is not None and result.get("asset_sha256") != asset_hash)
                or (prepared and (result.get("prepared") is not True or result.get("final_confirmation_available") is not True))):
            raise OperationsConflictError("browser_preparation_youtube_verification_failed")
        return result

    async def _youtube_steps(self, session, actor, action, row, bridge, state, metadata, *, fill):
        pins = {"platform": "youtube", "tab_id": row.tab_id, "run_id": row.id.hex,
            "expected_account_ref": state["expected_account_ref"]}
        if fill:
            observed = self._youtube_response(await bridge.request("media_prepare_youtube", phase="inspect", **pins),
                ref=state["expected_account_ref"])
            state["document_id"] = observed["document_id"]
            state["input_may_have_run"] = True
            await self._input_asset(session, actor, action, row, bridge, state)
            await self._check(session, actor, action, row, state, bridge)
            await self._save(session, actor, action, row, state, status="preparing", phase="metadata_fill_pending")
            await self._check(session, actor, action, row, state, bridge)
            result = await bridge.request("media_prepare_youtube", phase="fill", metadata=metadata,
                metadata_sha256=row.text_sha256, expected_document_id=state["document_id"], **pins)
            self._youtube_response(result, ref=state["expected_account_ref"], document=state["document_id"],
                metadata_hash=row.text_sha256, metadata=metadata, asset_hash=state["asset_manifest"]["sha256"])
        await self._check(session, actor, action, row, state, bridge, require_active=fill)
        verified = await bridge.request("media_prepare_youtube", phase="verify", metadata=metadata,
            metadata_sha256=row.text_sha256, expected_document_id=state["document_id"], **pins)
        self._youtube_response(verified, ref=state["expected_account_ref"], document=state["document_id"],
            metadata_hash=row.text_sha256, metadata=metadata, asset_hash=state["asset_manifest"]["sha256"], prepared=True)
        await self._check(session, actor, action, row, state, bridge, require_active=fill)
        await self._inspect_asset(row, bridge, state)

    async def _run(self, session, actor, action, row, bridge, state, text, *, fill):
        params = {"platform": "x", "expected_account_ref": state["expected_account_ref"], "tab_id": row.tab_id,
                  "run_id": row.id.hex}
        try:
            await self._check(session, actor, action, row, state, bridge, require_active=fill)
            if action.platform == "youtube":
                await self._youtube_steps(session, actor, action, row, bridge, state, text, fill=fill)
            elif state.get("asset_manifest") and not fill:
                # After an unknown attachment outcome, read the existing File
                # and draft only. Never stage/consume/input the asset again.
                await self._inspect_asset(row, bridge, state)
            else:
                await self._x_steps(session, actor, action, row, bridge, state, text, params, fill=fill)
            await self._check(session, actor, action, row, state, bridge, require_active=fill)
            # readonly復旧は停止中も証拠を読めるが、現在の入力権限なしに完了へ昇格させない。
            await self._require_active_action_binding(session, action, for_update=True)
            await self._save(session, actor, action, row, state, status="awaiting_final_confirmation", phase="verified_draft")
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._save(session, actor, action, row, state, status="uncertain", phase="stopped", error_code="browser_preparation_cancelled"))
            finally:
                raise
        except Exception as exc:
            # Input/transport outcomes after the dispatch checkpoint cannot be called failed/not-submitted.
            await session.rollback()
            await session.refresh(action)
            await session.refresh(row)
            try:
                await self._save(session, actor, action, row, state, status="uncertain" if state.get("input_may_have_run") or not fill else "blocked",
                    phase="stopped", error_code=str(exc)
                    if isinstance(exc, OperationsConflictError) and str(exc) in _CLOSED_UI_ERRORS.values()
                    else "browser_preparation_unverified")
            except OperationsConflictError:
                raise
        except BaseException:
            # Process-level interruption may occur after a consumed asset or
            # input dispatch. Preserve its durable ambiguity before propagating.
            try:
                await session.rollback()
                await session.refresh(action)
                await session.refresh(row)
                await asyncio.shield(self._save(session, actor, action, row, state,
                    status="uncertain", phase="stopped", error_code="browser_preparation_interrupted"))
            finally:
                raise
        return await self._safe(session, actor, action, row)

    async def _x_steps(self, session, actor, action, row, bridge, state, text, params, *, fill):
        observed = self._response(await bridge.request("media_prepare_text", phase="inspect", **params), ref=state["expected_account_ref"])
        if state.get("document_id") and state["document_id"] != observed["document_id"]:
            raise OperationsConflictError("browser_preparation_document_changed")
        state["document_id"] = observed["document_id"]
        if fill and observed.get("text_sha256") != row.text_sha256:
            if observed.get("text_sha256") != hashlib.sha256(b"").hexdigest():
                raise OperationsConflictError("browser_preparation_composer_not_empty")
            await self._check(session, actor, action, row, state, bridge)
            await self._save(session, actor, action, row, state, status="preparing", phase="fill_pending")
            # _save commits and releases the prior authority lock. Reacquire
            # fresh authority before dispatching the one permitted input.
            await self._check(session, actor, action, row, state, bridge)
            state["input_may_have_run"] = True
            filled = await bridge.request("media_prepare_text", phase="fill", text=text,
                expected_document_id=state["document_id"], **params)
            self._response(filled, ref=state["expected_account_ref"], document=state["document_id"], text_hash=row.text_sha256)
        await self._check(session, actor, action, row, state, bridge, require_active=fill)
        verified = await bridge.request("media_prepare_text", phase="verify", text=text,
            expected_document_id=state["document_id"], **params)
        self._response(verified, ref=state["expected_account_ref"], document=state["document_id"], text_hash=row.text_sha256, prepared=True)
        if state.get("asset_manifest"):
            await self._input_asset(session, actor, action, row, bridge, state)
            await self._check(session, actor, action, row, state, bridge)
            await self._inspect_asset(row, bridge, state)
