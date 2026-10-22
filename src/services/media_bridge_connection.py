"""Human-confirmed, secret-free binding of an existing account slot to Bridge.

Browser identity is draft-preparation evidence, never OAuth/provider authority.
The encrypted short-lived preview uses the existing checkpoint key provider.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from pc_bridge.edge import EdgeBridgeError
from .pc_bridge_store import PcBridgeError

from ..memory.models import (PlatformAccount, PlatformAccountRevision, Persona, PersonaRevision, ExternalConnection,
    MediaPlatformCredential, ExternalAction, ExternalActionApproval, MediaBrowserPreparation)
from ..security.media_credential_crypto import (decrypt_media_credential,
    probe_media_credential_key_readonly, encrypt_media_credential_readonly, MediaCredentialCryptoError, MediaCredentialKeyUnavailable)
from .media_browser_preparation import MediaBrowserPreparationService, PinnedMediaBridge, browser_context
from .media_operations_setup_service import MediaOperationsSetupService
from .operations_service import (OperationsAuthorizationError, OperationsConflictError,
    OperationsInvalidTransitionError, OperationsValidationError, sha256_json)

MODE = "bridge_manual"
TTL_SECONDS = 300
_REF = {"x": re.compile(r"@[A-Za-z0-9_]{1,15}\Z"), "youtube": re.compile(r"UC[A-Za-z0-9_-]{22}\Z")}


def manual_bridge(connection):
    return isinstance(getattr(connection, "metadata_json", None), dict) and connection.metadata_json.get("binding_mode") == MODE


class MediaBridgeConnectionService:
    def __init__(self, operations, *, bridge_factory=None):
        self.operations = operations
        self.browser = MediaBrowserPreparationService(operations, bridge_factory=bridge_factory)
        self.setup = MediaOperationsSetupService()

    async def _target(self, session, actor, account_id, expected, *, lock=False):
        owner = await self.operations._assert_human(actor)
        account = await self.operations._get_or_404(session, PlatformAccount, account_id, "platform_account_id", for_update=lock)
        await session.refresh(account, with_for_update=lock)
        await self.operations._assert_entity_access(session, actor, account, permission="write")
        if account.owner_user_id != owner or str(owner) != str(expected["owner_user_id"]):
            raise OperationsAuthorizationError("bridge_connection_owner_mismatch")
        if ((str(account.project_id) if account.project_id else None) != expected["project_id"]
                or str(account.persona_id) != expected["persona_id"]):
            raise OperationsConflictError("bridge_connection_persona_project_mismatch")
        if account.platform not in _REF or account.account_type != "profile":
            raise OperationsInvalidTransitionError("bridge_connection_platform_unsupported")
        persona = await self.operations._get_or_404(session, Persona, account.persona_id, "persona_id", for_update=lock)
        await session.refresh(persona, with_for_update=lock)
        if (persona.owner_user_id, persona.project_id) != (owner, account.project_id) or persona.state in {"paused", "archived"}:
            raise OperationsConflictError("bridge_connection_persona_unavailable")
        await self.operations._assert_entity_access(session, actor, persona, permission="write")
        persona_revision = await session.scalar(select(PersonaRevision).where(PersonaRevision.persona_id == persona.id)
            .order_by(PersonaRevision.version.desc()).limit(1).execution_options(populate_existing=True))
        if persona_revision is None or (persona_revision.owner_user_id, persona_revision.project_id) != (owner, account.project_id):
            raise OperationsConflictError("bridge_connection_persona_unavailable")
        current = await session.scalar(select(PlatformAccountRevision).where(PlatformAccountRevision.platform_account_id == account.id)
            .order_by(PlatformAccountRevision.version.desc()).limit(1).execution_options(populate_existing=True))
        if (current is None or str(current.id) != expected["expected_account_revision_id"]
                or current.content_hash != expected["expected_account_revision_hash"]
                or current.content_hash != self.operations._media_account_revision_content_hash(current)
                or (current.owner_user_id, current.project_id) != (owner, account.project_id)):
            raise OperationsConflictError("bridge_connection_account_revision_changed")
        if await session.scalar(select(MediaPlatformCredential.id).where(MediaPlatformCredential.platform_account_id == account.id).limit(1)):
            raise OperationsInvalidTransitionError("bridge_connection_existing_vault_binding")
        connection = None
        if account.connection_id:
            connection = await self.operations._get_or_404(session, ExternalConnection, account.connection_id, "connection_id", for_update=lock)
            await session.refresh(connection, with_for_update=lock)
            if (not manual_bridge(connection) or connection.credential_ref is not None
                    or (connection.owner_user_id, connection.project_id) != (owner, account.project_id)
                    or connection.provider_key != account.platform or connection.remote_account_ref != account.account_ref
                    or connection.auth_status != "unknown"):
                raise OperationsInvalidTransitionError("bridge_connection_existing_binding_unavailable")
        snapshot = {"owner_user_id": str(owner), "project_id": str(account.project_id) if account.project_id else None,
            "persona_id": str(persona.id), "persona_state": persona.state, "platform": account.platform,
            "persona_revision_id": str(persona_revision.id), "persona_revision_hash": persona_revision.content_hash,
            "persona_display_name": persona_revision.display_name,
            "account_id": str(account.id), "account_ref": account.account_ref, "account_status": account.status,
            "expected_account_revision_id": str(current.id), "expected_account_revision_hash": current.content_hash,
            "connection_id": str(connection.id) if connection else None, "connection_version": connection.version if connection else None}
        return account, persona, current, connection, snapshot

    async def _identity(self, bridge, platform, ref, tab, account_id):
        context = await self.browser._require_capabilities(bridge, platform, has_asset=False, account_id=account_id)
        bridge = PinnedMediaBridge(bridge, context, platform=platform, account_id=account_id)
        response = await bridge.request("media_prepare_readiness", platform=platform, tab_id=tab, expected_account_ref=ref)
        keys = {"platform", "account_ref", "account_ref_kind", "document_id", "helper_revision", "composer_ready",
            "account_verified", "available", "executed", "publication_confirmed", "outcome_unknown"}
        if (not isinstance(response, dict) or set(response) != keys or response["platform"] != platform
                or response["account_ref"] != ref or response["account_ref_kind"] != ("x_handle" if platform == "x" else "youtube_channel_id")
                or type(response["helper_revision"]) is not int or response["helper_revision"] != (9 if platform == "x" else 4)
                or not isinstance(response["document_id"], str) or not 1 <= len(response["document_id"]) <= 128
                or any(response[k] is not True for k in ("composer_ready", "account_verified", "available"))
                or any(response[k] is not False for k in ("executed", "publication_confirmed", "outcome_unknown"))):
            raise OperationsInvalidTransitionError("bridge_connection_identity_unverified")
        if browser_context(await bridge.request("media_prepare_context")) != context:
            raise OperationsConflictError("bridge_connection_browser_context_changed")
        return response["document_id"], context

    async def _bridge(self, owner, device):
        bridge = await self.browser._bridge(owner, device)
        if getattr(bridge, "owner", None) != owner or getattr(bridge, "device_id", None) != device:
            raise OperationsAuthorizationError("bridge_connection_device_owner_mismatch")
        lock = getattr(bridge, "operation_lock", None)
        if lock is None or not lock.acquire(blocking=False):
            raise OperationsConflictError("bridge_connection_device_busy")
        return bridge, lock

    async def preview(self, session, actor, account_id, **body):
        expected = {k: str(body[k]) if body[k] is not None else None for k in
            ("owner_user_id", "project_id", "persona_id", "expected_account_revision_id", "expected_account_revision_hash")}
        account, persona, current, _, snapshot = await self._target(session, actor, account_id, expected)
        ref, device, tab = body["account_ref"], body["device_id"], body["tab_id"]
        if (not isinstance(ref, str) or not _REF[account.platform].fullmatch(ref)
                or not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", device)
                or type(tab) is not int or tab < 1):
            raise OperationsValidationError("bridge_connection_explicit_target_required")
        ref = ref.lower() if account.platform == "x" else ref
        labels = {key: body.get(key) for key in ("browser_context_label", "google_account_label")}
        for key, value in labels.items():
            if key == "google_account_label" and account.platform == "x" and value is None:
                continue
            if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise OperationsValidationError("bridge_connection_explicit_account_labels_required")
            labels[key] = value.strip()
        if account.platform == "x" and labels["google_account_label"] is not None:
            raise OperationsValidationError("bridge_connection_google_label_platform_mismatch")
        try:
            if not probe_media_credential_key_readonly():
                raise MediaCredentialKeyUnavailable("unavailable")
            bridge, lock = await self._bridge(str(account.owner_user_id), device)
            try:
                epoch = self.browser._epoch(bridge)
                document, context = await self._identity(bridge, account.platform, ref, tab, account.id)
                _, _, _, _, fresh = await self._target(session, actor, account_id, expected)
                if fresh != snapshot or self.browser._epoch(bridge) != epoch:
                    raise OperationsConflictError("bridge_connection_target_changed")
                now = datetime.utcnow()
                proof = {"purpose": "media.bridge-connection.v2", "snapshot": snapshot, "account_ref": ref,
                    "browser_context": context, **labels,
                    "device_id": device, "tab_id": tab, "document_id": document, "epoch": epoch,
                    "expires_at": (now + timedelta(seconds=TTL_SECONDS)).replace(tzinfo=timezone.utc).timestamp()}
                token = encrypt_media_credential_readonly(proof, credential_id=account.id, platform_account_id=account.id).ciphertext
            finally:
                lock.release()
        except (MediaCredentialCryptoError, MediaCredentialKeyUnavailable):
            raise OperationsInvalidTransitionError("bridge_connection_checkpoint_key_unavailable") from None
        except (TimeoutError, PcBridgeError, EdgeBridgeError):
            raise OperationsInvalidTransitionError("bridge_connection_identity_unverified") from None
        return {"mode": MODE, "owner_user_id": str(account.owner_user_id), "project_id": expected["project_id"],
            "persona_id": str(persona.id), "persona_display_name": snapshot["persona_display_name"], "platform_account_id": str(account.id),
            "platform_account_revision_id": str(current.id), "platform_account_revision_hash": current.content_hash,
            "platform": account.platform, "account_display_name": current.display_name, "account_ref": ref,
            "device_id": device, "tab_id": tab, "browser_context": context, **labels, "checked_at": now.isoformat() + "Z",
            "expires_at": datetime.utcfromtimestamp(proof["expires_at"]).isoformat() + "Z", "preview_token": token,
            "official_api_verified": False, "publication_confirmed": False}

    async def save(self, session, actor, account_id, *, preview_token, confirmed, idempotency_key):
        if confirmed is not True or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 220:
            raise OperationsValidationError("bridge_connection_explicit_confirmation_required")
        try:
            proof = decrypt_media_credential(preview_token, credential_id=account_id, platform_account_id=account_id)
            if proof["purpose"] != "media.bridge-connection.v2" or proof["expires_at"] <= datetime.now(timezone.utc).timestamp():
                raise ValueError()
            expected = proof["snapshot"]
        except (MediaCredentialCryptoError, MediaCredentialKeyUnavailable, ValueError, KeyError, TypeError):
            raise OperationsConflictError("bridge_connection_preview_expired_or_invalid") from None
        bridge = lock = None
        try:
            account, _, _, _, snapshot = await self._target(session, actor, account_id, expected)
            if snapshot != expected:
                raise OperationsConflictError("bridge_connection_target_changed")
            bridge, lock = await self._bridge(str(account.owner_user_id), proof["device_id"])
            epoch = self.browser._epoch(bridge)
            document, context = await self._identity(bridge, account.platform, proof["account_ref"], proof["tab_id"], account.id)
            if context != proof["browser_context"]:
                raise OperationsConflictError("bridge_connection_browser_context_changed")
            if epoch != proof["epoch"] or self.browser._epoch(bridge) != epoch or document != proof["document_id"]:
                raise OperationsConflictError("bridge_connection_document_or_device_changed")
            # Read-only Bridge I/O finishes before any DB row lock. Match the
            # Action-first order used by approve/prepare/inspect/checkpoints.
            actions = await self.operations._lock_media_account_actions(session, account.id)
            account, persona, current, connection, fresh = await self._target(session, actor, account_id, expected, lock=True)
            if fresh != expected or self.browser._epoch(bridge) != epoch:
                raise OperationsConflictError("bridge_connection_target_changed")
            # A fenced new proposal can commit between the Action scan and
            # Account lock. Never acquire its Action lock in reverse order;
            # reject this save and require a fresh preview instead.
            current_action_ids = set((await session.scalars(select(ExternalAction.id)
                .where(ExternalAction.platform_account_id == account.id))).all())
            if current_action_ids != {action.id for action in actions}:
                raise OperationsConflictError("bridge_connection_actions_changed")
            if proof["expires_at"] <= datetime.now(timezone.utc).timestamp():
                raise OperationsConflictError("bridge_connection_preview_expired_or_invalid")
            if await session.scalar(select(PlatformAccountRevision.id).where(PlatformAccountRevision.platform_account_id == account.id,
                    PlatformAccountRevision.idempotency_key == "bridge-bind:" + idempotency_key).limit(1)):
                raise OperationsConflictError("bridge_connection_already_saved")
            if any((a.owner_user_id, a.project_id) != (account.owner_user_id, account.project_id) for a in actions):
                raise OperationsConflictError("bridge_connection_action_scope_changed")
            if any(a.status in {"running", "attempting", "uncertain"} for a in actions) or await session.scalar(select(MediaBrowserPreparation.id).where(
                    MediaBrowserPreparation.platform_account_id == account.id, MediaBrowserPreparation.status.in_(["preparing", "uncertain"])).limit(1)):
                raise OperationsConflictError("bridge_connection_reconciliation_required")
            # No vault row, grant, token, or provider verification is created.
            revision_id = uuid4()
            if connection is None:
                connection = ExternalConnection(id=uuid4(), owner_user_id=account.owner_user_id, project_id=account.project_id,
                    provider_key=account.platform, display_name=current.display_name, version=1)
                session.add(connection)
            else:
                connection.version += 1
            connection.remote_account_ref = proof["account_ref"]
            connection.auth_status = "unknown"
            connection.credential_ref = None
            connection.metadata_json = {"binding_mode": MODE, "persona_id": str(persona.id), "platform_account_id": str(account.id),
                "binding_revision_id": str(revision_id), "device_id": proof["device_id"], "tab_id": proof["tab_id"],
                "browser_context": context, "browser_context_label": proof["browser_context_label"],
                "google_account_label": proof["google_account_label"], "google_identity_verified": False,
                "document_sha256": hashlib.sha256(document.encode()).hexdigest(),
                "epoch_sha256": hashlib.sha256(str(epoch).encode()).hexdigest(), "identity_source": "owner_bridge_current_tab"}
            # There is no ORM relationship defining this dependency. PostgreSQL
            # enforces the FK immediately, so insert Connection before updating
            # the existing Account's connection_id (SQLite may not enforce it).
            await session.flush([connection])
            account.account_ref = proof["account_ref"]
            account.connection_id = connection.id
            account.status = "active"
            content = {k: current.to_safe_dict()[k] for k in ("display_name", "publish_capability", "media_capability",
                "analytics_capability", "credential_status", "remote_url", "locale", "timezone", "supported_content_modes",
                "disclosure_defaults", "rating_defaults", "adapter_ref")}
            content.update(publish_capability="unknown", media_capability="unknown", analytics_capability="unknown",
                credential_status="not_configured", adapter_ref="bridge_manual:v1")
            revision = self.setup._build_platform_revision(account=account, version=current.version + 1,
                content=content, created_by=account.owner_user_id, idempotency_key="bridge-bind:" + idempotency_key)
            revision.id = revision_id
            session.add(revision)
            for action in actions:
                approvals = list((await session.scalars(select(ExternalActionApproval).where(ExternalActionApproval.action_id == action.id,
                    ExternalActionApproval.action_version == action.action_version, ExternalActionApproval.decision == "approved"))).all())
                for approval in approvals:
                    session.add(ExternalActionApproval(action_id=action.id, owner_user_id=action.owner_user_id,
                        action_version=approval.action_version, payload_hash=approval.payload_hash, artifact_hashes=approval.artifact_hashes,
                        decision="invalidated", reason="Bridge account binding changed", decided_by=account.owner_user_id))
                if action.status in {"proposed", "pending_approval", "approved"}:
                    action.status = "rejected"
                    action.version += 1
            receipt = {"binding_revision_id": str(revision.id), "account_revision_hash": revision.content_hash,
                "connection_id": str(connection.id), "connection_version": connection.version,
                "owner_user_id": str(account.owner_user_id), "project_id": str(account.project_id) if account.project_id else None,
                "persona_id": str(persona.id), "platform_account_id": str(account.id), "platform": account.platform,
                "account_ref": proof["account_ref"], "device_id": proof["device_id"], "tab_id": proof["tab_id"],
                "browser_context": context, "browser_context_label": proof["browser_context_label"],
                "google_account_label": proof["google_account_label"],
                "saved_at": datetime.now(timezone.utc).isoformat()}
            # Preserve the exact save receipt in the existing append-only audit
            # event, separately from mutable current Connection metadata.
            await self.operations._event(session, actor=actor, entity_type="platform_account", entity_id=account.id,
                event_type="media.bridge_connection.saved", owner_user_id=account.owner_user_id, project_id=account.project_id,
                payload={"binding_mode": MODE, "persona_id": str(persona.id), "revision_id": str(revision.id),
                    "connection_id": str(connection.id), "binding_receipt": receipt, "official_api_verified": False})
            await session.flush()
            if self.browser._epoch(bridge) != epoch:
                raise OperationsConflictError("bridge_connection_target_changed")
            saved_account = self._account_snapshot(account, revision)
            await session.commit()
            result = await self._readback(session, saved_account, receipt)
            # A profile can switch without reconnecting the PC WebSocket. The
            # immutable receipt stays ours; never certify its current context.
            try:
                result["readback_binding_matches"] &= browser_context(await bridge.request("media_prepare_context")) == context
            except (PcBridgeError, EdgeBridgeError, TimeoutError, OperationsInvalidTransitionError):
                result["readback_binding_matches"] = False
            return result
        except (IntegrityError, OperationalError):
            await session.rollback()
            raise OperationsConflictError("bridge_connection_concurrent_or_duplicate_save") from None
        except (TimeoutError, PcBridgeError, EdgeBridgeError):
            await session.rollback()
            raise OperationsInvalidTransitionError("bridge_connection_identity_unverified") from None
        except Exception:
            await session.rollback()
            raise
        finally:
            if lock is not None:
                lock.release()

    @staticmethod
    def _account_snapshot(account, revision):
        current = revision.to_safe_dict()
        return {**account.to_safe_dict(), "current_revision": current, "revisions": [current],
            "revision_history_truncated": revision.version > 1}

    async def _readback(self, session, saved_account, receipt):
        # One SQL snapshot reads all three pins. A later binding never becomes
        # the receipt for this save, even when its remote account_ref is equal.
        latest_id = select(PlatformAccountRevision.id).where(PlatformAccountRevision.platform_account_id == PlatformAccount.id)
        latest_id = latest_id.order_by(PlatformAccountRevision.version.desc()).limit(1).correlate(PlatformAccount).scalar_subquery()
        row = (await session.execute(select(PlatformAccount, ExternalConnection, PlatformAccountRevision)
            .outerjoin(ExternalConnection, ExternalConnection.id == PlatformAccount.connection_id)
            .outerjoin(PlatformAccountRevision, PlatformAccountRevision.id == latest_id)
            .where(PlatformAccount.id == UUID(saved_account["id"]))
            .execution_options(populate_existing=True))).first()
        account = saved_account
        matches = False
        if row is not None:
            current, connection, revision = row
            same_scope = ((str(current.owner_user_id), str(current.project_id) if current.project_id else None, str(current.persona_id))
                == (receipt["owner_user_id"], receipt["project_id"], receipt["persona_id"]))
            if same_scope and revision is not None:
                account = self._account_snapshot(current, revision)
                metadata = connection.metadata_json if connection is not None and isinstance(connection.metadata_json, dict) else {}
                matches = bool(current.status == "active" and current.account_ref == receipt["account_ref"]
                    and current.platform == receipt["platform"] and str(current.connection_id) == receipt["connection_id"]
                    and str(revision.id) == receipt["binding_revision_id"] and revision.content_hash == receipt["account_revision_hash"]
                    and connection is not None and connection.version == receipt["connection_version"]
                    and connection.owner_user_id == current.owner_user_id and connection.project_id == current.project_id
                    and connection.remote_account_ref == receipt["account_ref"] and connection.provider_key == receipt["platform"]
                    and connection.auth_status == "unknown" and connection.credential_ref is None and manual_bridge(connection)
                    and all(metadata.get(k) == receipt[k] for k in ("binding_revision_id", "device_id", "tab_id", "persona_id", "platform_account_id",
                        "browser_context", "browser_context_label", "google_account_label")))
        return {"mode": MODE, "account": account, "binding_receipt": receipt, "readback_binding_matches": matches,
            "official_api_verified": False, "publication_confirmed": False}
