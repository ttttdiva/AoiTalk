"""Official, account-scoped OAuth consent; tokens only enter the existing vault.

Configuration is server-side AOITALK_MEDIA_OAUTH_CONFIG_JSON, keyed by brand
Project UUID then provider. No default/shared-project fallback is accepted.
The frontend receives the consent URL and relays code/state via authenticated
POST after immediately clearing its callback URL; this service has no GET
callback that could expose authorization codes in access logs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx
from ..security.http_log_redaction import install_http_log_redaction

install_http_log_redaction()
from sqlalchemy import select, update

from ..memory.models.media_operations_credentials import MediaPlatformOAuthRequest
from ..security.media_credential_crypto import decrypt_media_credential, encrypt_media_credential
from .media_credential_package import CredentialPackage
from .media_credential_provider_verifier import MediaCredentialProviderVerifier, has_trusted_identity_evidence
from .media_credential_vault_service import MediaCredentialVaultService
from .media_operations_service import MediaOperationsConflictError, MediaOperationsValidationError, _actor_id


OAUTH_SCOPES = {
    "x": ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"],
    "youtube": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
}
AUTHORIZE_URLS = {"x": "https://x.com/i/oauth2/authorize", "youtube": "https://accounts.google.com/o/oauth2/v2/auth"}
TOKEN_URLS = {"x": "https://api.x.com/2/oauth2/token", "youtube": "https://oauth2.googleapis.com/token"}
MAX_TOKEN_RESPONSE = 64 * 1024


def _config(project_id, platform):
    try:
        all_config = json.loads(os.getenv("AOITALK_MEDIA_OAUTH_CONFIG_JSON", "{}"))
        value = all_config.get(str(project_id), {}).get(platform, {})
        if not isinstance(value, dict):
            return {}
        return value
    except (ValueError, AttributeError):
        return {}


def _config_blockers(value):
    missing = [f"oauth_{key}_missing" for key in ("client_id", "client_secret", "redirect_uri")
               if not isinstance(value.get(key), str) or not value[key].strip()]
    if not missing:
        parsed = urlparse(value["redirect_uri"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            missing.append("oauth_redirect_uri_invalid")
    return missing


def _config_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MediaPlatformOAuthService:
    def __init__(self, *, vault=None, verifier=None, config_loader=_config, client_factory=None):
        self.vault = vault or MediaCredentialVaultService()
        self.verifier = verifier or MediaCredentialProviderVerifier()
        self.config_loader = config_loader
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False))

    async def _binding(self, session, actor, account_id, *, require_active=True):
        await self.vault._assert_human(actor)
        account = await self.vault._account(session, actor, account_id, write=True, lock=True)
        await session.refresh(account)
        if account.platform not in OAUTH_SCOPES or account.project_id is None or (require_active and account.status != "active"):
            raise MediaOperationsValidationError("OAuth requires an active X/YouTube account in its brand Project")
        revision = await self.vault._latest_revision(session, account.id, lock=True)
        credential = await self.vault._credential(session, account.id, lock=True)
        if credential is not None:
            await session.refresh(credential)
        return account, revision, credential

    async def metadata(self, session, actor, account_id):
        account, revision, credential = await self._binding(session, actor, account_id, require_active=False)
        config = self.config_loader(account.project_id, account.platform)
        blockers = _config_blockers(config)
        if account.status != "active":
            blockers.append("platform_account_inactive")
        return {"platform_account_id": str(account.id), "project_id": str(account.project_id),
                "platform": account.platform, "account_ref": account.account_ref,
                "account_revision": revision.version, "credential_revision": credential.revision if credential else 0,
                "configured": not blockers, "blockers": blockers, "required_scopes": OAUTH_SCOPES[account.platform],
                "redirect_uri": config.get("redirect_uri") if not blockers else None,
                "client_ref": hashlib.sha256(config["client_id"].encode()).hexdigest()[:16] if not blockers else None,
                "external_checks": ["developer_app_and_api_access", "credits_and_rate_limits"] if account.platform == "x"
                  else ["youtube_api_enabled", "oauth_consent_verified", "upload_project_audit_for_public_visibility", "custom_thumbnail_channel_permission"]}

    async def start(self, session, actor, account_id, *, expected_account_revision, expected_credential_revision):
        account, revision, credential = await self._binding(session, actor, account_id)
        if account.connection_id:
            from ..memory.models import ExternalConnection
            from .media_bridge_connection import manual_bridge
            if manual_bridge(await session.get(ExternalConnection, account.connection_id)):
                raise MediaOperationsConflictError("Bridge manual identity does not authorize an OAuth grant")
        actual_credential_revision = credential.revision if credential else 0
        if revision.version != expected_account_revision or actual_credential_revision != expected_credential_revision:
            raise MediaOperationsConflictError("OAuth account or credential revision changed")
        config = self.config_loader(account.project_id, account.platform)
        if _config_blockers(config):
            raise MediaOperationsConflictError("OAuth client settings are incomplete for this brand Project")
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        request_id = uuid4()
        encrypted = encrypt_media_credential({"state": nonce, "verifier": verifier, "config_hash": _config_hash(config),
            "account_ref": account.account_ref},
            credential_id=f"oauth-request:{request_id}", platform_account_id=account.id)
        row = MediaPlatformOAuthRequest(id=request_id, owner_user_id=account.owner_user_id, created_by=_actor_id(actor),
            project_id=account.project_id, platform_account_id=account.id, account_revision_id=revision.id,
            credential_revision=actual_credential_revision, platform=account.platform,
            state_digest=hashlib.sha256(nonce.encode()).hexdigest(), encrypted_request=encrypted.ciphertext,
            status="pending", expires_at=datetime.utcnow() + timedelta(minutes=10))
        session.add(row)
        await session.commit()
        params = {"response_type": "code", "client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
                  "scope": " ".join(OAUTH_SCOPES[account.platform]), "state": nonce,
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
                  "code_challenge_method": "S256"}
        if account.platform == "youtube":
            params.update(access_type="offline", prompt="consent", include_granted_scopes="false")
        return {"authorization_url": AUTHORIZE_URLS[account.platform] + "?" + urlencode(params),
                "expires_at": row.expires_at.isoformat(), "platform_account_id": str(account.id),
                "project_id": str(account.project_id), "platform": account.platform, "account_ref": account.account_ref}

    async def callback(self, session, actor, *, state, code=None, error=None):
        await self.vault._assert_human(actor)
        if not isinstance(state, str) or not 32 <= len(state) <= 500:
            raise MediaOperationsValidationError("OAuth callback is invalid")
        row = await session.scalar(select(MediaPlatformOAuthRequest).where(
            MediaPlatformOAuthRequest.state_digest == hashlib.sha256(state.encode()).hexdigest()).with_for_update())
        if row is None or row.created_by != _actor_id(actor) or row.status != "pending" or row.expires_at <= datetime.utcnow():
            raise MediaOperationsConflictError("OAuth state is invalid, expired or already consumed")
        account, revision, credential = await self._binding(session, actor, row.platform_account_id)
        if (row.owner_user_id != account.owner_user_id or row.project_id != account.project_id or row.platform != account.platform
            or row.account_revision_id != revision.id or row.credential_revision != (credential.revision if credential else 0)):
            raise MediaOperationsConflictError("OAuth account binding changed")
        config = self.config_loader(account.project_id, account.platform)
        secret = decrypt_media_credential(row.encrypted_request, credential_id=f"oauth-request:{row.id}", platform_account_id=account.id)
        if _config_blockers(config) or secret.get("config_hash") != _config_hash(config):
            raise MediaOperationsConflictError("OAuth client settings changed")
        if error is None and (not isinstance(code, str) or not 1 <= len(code) <= 8192):
            raise MediaOperationsValidationError("OAuth callback code is invalid")
        claimed = await session.execute(update(MediaPlatformOAuthRequest).where(
            MediaPlatformOAuthRequest.id == row.id, MediaPlatformOAuthRequest.status == "pending",
        ).values(status="failed" if error else "exchanging").execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise MediaOperationsConflictError("OAuth state was already consumed")
        await session.commit()
        if error:
            return {"status": "consent_denied", "platform_account_id": str(account.id), "credential": None}
        request_id, account_uuid = row.id, account.id
        token = {}
        try:
            data = {"grant_type": "authorization_code", "code": code, "redirect_uri": config["redirect_uri"],
                    "code_verifier": secret["verifier"]}
            if account.platform == "youtube":
                data.update(client_id=config["client_id"], client_secret=config["client_secret"])
            kwargs = {"data": data}
            if account.platform == "x":
                kwargs["auth"] = httpx.BasicAuth(config["client_id"], config["client_secret"])
            async with self.client_factory() as client:
                async with client.stream("POST", TOKEN_URLS[account.platform], **kwargs) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_TOKEN_RESPONSE:
                            raise ValueError("oversized token response")
                    if response.status_code != 200:
                        raise ValueError("token exchange rejected")
                    token = json.loads(raw)
            if (not isinstance(token, dict) or not isinstance(token.get("access_token"), str)
                or not token["access_token"] or str(token.get("token_type", "")).lower() != "bearer"):
                raise ValueError("invalid token response")
            payload = {key: token[key] for key in ("access_token", "refresh_token", "token_type", "scope") if key in token}
            # Store only provider-reported grants. An omitted scope response
            # may verify identity, but must not imply publication readiness.
            if type(token.get("expires_in")) is int and token["expires_in"] > 0:
                payload["expires_at"] = int(time.time()) + token["expires_in"]
            package = CredentialPackage(platform=account.platform, connection_type="oauth", payload=payload)
            verified = await self.verifier.verify(package, account_ref=account.account_ref)
            if verified.status != "verified" or not has_trusted_identity_evidence(verified):
                raise ValueError("provider identity did not match")
            # Check again after I/O. A concurrently changed brand/credential wins.
            account, latest, current = await self._binding(session, actor, account.id)
            if (row.owner_user_id != account.owner_user_id or row.project_id != account.project_id
                or row.platform != account.platform or latest.id != row.account_revision_id
                or (current.revision if current else 0) != row.credential_revision
                or account.account_ref != secret.get("account_ref")):
                raise ValueError("binding changed during exchange")
            package_bytes = json.dumps(payload)
            if current is None:
                saved = await self.vault.create_credential(session, actor, account.id, package=package_bytes,
                    connection_type="oauth", idempotency_key=f"oauth-add-{row.id}")
            else:
                saved = await self.vault.rotate_credential(session, actor, account.id, expected_revision=current.revision,
                    package=package_bytes, connection_type="oauth", idempotency_key=f"oauth-rotate-{row.id}")
            class Preverified:
                async def verify(self, candidate, *, account_ref):
                    if candidate.payload != payload or account_ref != account.account_ref:
                        raise MediaOperationsConflictError("OAuth identity binding changed")
                    return verified
            saved = await self.vault.verify_credential(session, actor, account.id, expected_revision=saved["revision"],
                idempotency_key=f"oauth-verify-{row.id}", verifier=Preverified())
            if saved.get("status") != "verified":
                raise ValueError("saved credential was not verified")
            await session.execute(update(MediaPlatformOAuthRequest).where(MediaPlatformOAuthRequest.id == row.id).values(status="completed"))
            await session.commit()
            return {"status": "connected", "platform_account_id": str(account.id), "credential": saved}
        except Exception:
            await session.rollback()
            await session.execute(update(MediaPlatformOAuthRequest).where(MediaPlatformOAuthRequest.id == request_id).values(status="failed"))
            await session.commit()
            return {"status": "connection_failed", "platform_account_id": str(account_uuid), "credential": None}
        finally:
            if isinstance(token, dict):
                token.clear()
            secret.clear()
