"""Fail-closed Generation Studio integration for MediaOps WS4.

Only semantic image-generation requests and opaque Studio receipts cross this
boundary.  The service never accepts provider workflow graphs, credentials,
filesystem paths, or raw provider response blobs.  Submission is fenced by a
durable intent committed *before* the external call; an uncertain call is
never silently retried.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import httpx

from ..memory.models import (
    ContentItem,
    ContentVariant,
    ContentVariantRevision,
    CreativeRecipe,
    CreativeRecipeRevision,
    GenerationOutput,
    GenerationOutputSelection,
    GenerationPlan,
    GenerationRun,
    GenerationRunIntent,
    GenerationRunObservation,
    GenerationWorkspace,
    Persona,
    PersonaRevision,
)
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsService,
    MediaOperationsValidationError,
    _as_uuid,
    _bounded_page,
    _idempotency_key,
    _optional_text,
    _required_text,
    _validated_resource_url,
    _validated_sha256,
    sha256_json,
)


class GenerationStudioAdapter(Protocol):
    """Narrow adapter contract for the 73 Generation Studio boundary."""

    async def get_image_catalog(
        self,
        binding: "StudioScope",
    ) -> Mapping[str, Any]: ...

    async def get_video_catalog(
        self,
        binding: "StudioScope",
    ) -> Mapping[str, Any]: ...

    async def submit_image(
        self,
        binding: "StudioScope",
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def submit_video(
        self,
        binding: "StudioScope",
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def get_preset_catalog(self, binding: "StudioScope") -> Mapping[str, Any]: ...

    async def submit_preset_image(
        self,
        binding: "StudioScope",
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def get_preset_run(
        self,
        binding: "StudioScope",
        external_run_id: str,
    ) -> Mapping[str, Any]: ...

    async def get_run(
        self,
        binding: "StudioScope",
        external_run_id: str,
    ) -> Mapping[str, Any]: ...

    async def reconcile_video(
        self,
        binding: "StudioScope",
        external_run_id: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class StudioScope:
    """Opaque external scope.  No URLs, paths, or credentials are accepted."""

    workspace_id: str
    project_id: str | None = None
    # The origin is configuration, not a provider credential.  It is kept out
    # of generated request/receipt payloads and is validated before use.
    base_url: str | None = None


class UnavailableGenerationStudioAdapter:
    """Default adapter: never pretends an unavailable provider succeeded."""

    async def get_image_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        return {"status": "unavailable", "items": []}

    async def get_video_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        return {"status": "unavailable", "items": []}

    async def submit_image(
        self,
        binding: StudioScope,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}

    async def submit_video(
        self,
        binding: StudioScope,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}

    async def get_preset_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        return {"status": "unavailable", "items": []}

    async def submit_preset_image(
        self, binding: StudioScope, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}

    async def get_preset_run(
        self,
        binding: StudioScope,
        external_run_id: str,
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}

    async def get_run(
        self,
        binding: StudioScope,
        external_run_id: str,
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}

    async def reconcile_video(
        self,
        binding: StudioScope,
        external_run_id: str,
    ) -> Mapping[str, Any]:
        return {"status": "unavailable", "reason_code": "adapter_unavailable"}


class HttpGenerationStudioAdapter:
    """Small HTTP adapter for the supported 73 Generation Studio API.

    AoiTalk owns only this semantic boundary.  The bearer token is read from
    process configuration and is never placed in MediaOps rows, DTOs, logs, or
    the adapter response.  The adapter intentionally records a terminal 73
    success as ``output_pending`` until immutable output metadata is available;
    a job status by itself is not proof of a usable generated artifact.
    """

    def __init__(self, *, token: str | None = None, timeout: float = 30.0) -> None:
        configured = token
        if configured is None:
            configured = (
                os.getenv("AOITALK_GENERATION_STUDIO_BEARER_TOKEN")
                or os.getenv("GENERATION_STUDIO_BEARER_TOKEN")
            )
        self.token = configured.strip() if isinstance(configured, str) else None
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @staticmethod
    def _origin(binding: StudioScope) -> str:
        if not binding.base_url:
            raise ValueError("Generation Studio base URL is not configured")
        return _validate_safe_url(binding.base_url, "base_url", origin_only=True)

    async def _json_request(
        self,
        method: str,
        binding: StudioScope,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not self.configured:
            return {"status": "unavailable", "reason_code": "adapter_token_missing"}
        origin = self._origin(binding)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    f"{origin}{path}",
                    json=dict(payload) if payload is not None else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            # Deliberately do not include exception text: it can contain a URL
            # or transport details that are not part of the MediaOps contract.
            raise RuntimeError("generation_studio_transport_error") from exc
        if response.status_code in {401, 403}:
            return {"status": "unavailable", "reason_code": "studio_auth_failed"}
        if response.status_code == 404:
            return {"status": "unavailable", "reason_code": "studio_endpoint_unavailable"}
        if response.status_code >= 500:
            raise RuntimeError("generation_studio_server_error")
        if response.status_code < 200 or response.status_code >= 300:
            return {"status": "failed", "reason_code": "studio_request_rejected"}
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("generation_studio_invalid_response") from exc
        if not isinstance(body, Mapping):
            raise RuntimeError("generation_studio_invalid_response")
        return body

    @staticmethod
    def _status(raw: Any) -> str:
        status = str(raw or "").strip().lower()
        # 73 uses succeeded for a job whose output metadata is still exposed
        # only through the asset boundary.  Keep that distinction explicit.
        if status in {"complete", "completed", "success"}:
            return "succeeded"
        if status in _STATUSES:
            return status
        return "uncertain"

    @classmethod
    def _project(cls, body: Mapping[str, Any], *, fallback_run_id: str | None = None) -> dict[str, Any]:
        run_id = body.get("run_id") or body.get("job_id") or fallback_run_id
        status = cls._status(body.get("status"))
        asset_ids = body.get("output_asset_ids")
        version_ids = body.get("output_version_ids")
        if not isinstance(asset_ids, Sequence) or isinstance(asset_ids, (str, bytes)):
            asset_ids = []
        if not isinstance(version_ids, Sequence) or isinstance(version_ids, (str, bytes)):
            version_ids = []
        # No 73 public job response contains immutable sha256/mime metadata.
        # Therefore a success with IDs but without detailed outputs remains
        # pending rather than being falsely persisted as usable success.
        outputs = body.get("outputs")
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            outputs = []
        if status == "succeeded" and not outputs:
            status = "output_pending"
        projected: dict[str, Any] = {
            "status": status,
            "run_id": run_id,
            "workspace_id": body.get("workspace_id"),
            "project_id": body.get("project_id"),
            "executor_kind": body.get("executor_kind"),
            "progress": body.get("progress"),
            "attempt": body.get("attempt"),
            "cancel_requested": body.get("cancel_requested"),
            "output_asset_ids": list(asset_ids)[:20],
            "output_version_ids": list(version_ids)[:20],
            "outputs": list(outputs)[:20],
            "adapter_release": body.get("adapter_release") or "73_generation_studio_http",
            "started_at": body.get("started_at") or body.get("created_at"),
            "finished_at": body.get("finished_at"),
            "cost_summary": body.get("cost_summary") or {},
            "error_code": body.get("error_code") or body.get("reason_code"),
            "result_deep_link": body.get("result_deep_link"),
        }
        return projected

    async def get_image_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        body = await self._json_request("GET", binding, "/api/v1/image-generation/catalog")
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        # Keep the top-level flattened selections; list_image_catalog applies
        # the strict field projection before exposing it to AoiTalk clients.
        return body

    async def get_preset_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        body = await self._json_request("GET", binding, "/api/v1/external/presets")
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        items = body.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise RuntimeError("generation_studio_invalid_preset_catalog")
        safe: list[dict[str, Any]] = []
        allowed = {
            "preset_id", "name", "description", "current_revision_id",
            "current_revision", "checksum", "model_family", "updated_at",
        }
        for item in items[:200]:
            if not isinstance(item, Mapping) or set(item) - allowed:
                raise RuntimeError("generation_studio_invalid_preset_catalog")
            safe.append({key: item.get(key) for key in allowed if key in item})
        return {"status": "verified", "items": safe}

    async def submit_preset_image(
        self, binding: StudioScope, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        body = await self._json_request(
            "POST", binding, "/api/v1/external/image-generations", payload=request
        )
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        allowed = {
            "run_id", "status", "preset_id", "preset_revision_id",
            "revision_number", "checksum", "output_asset_ids",
            "output_version_ids", "error_code", "result_deep_link",
        }
        if set(body) - allowed:
            raise RuntimeError("generation_studio_invalid_external_receipt")
        return dict(body)

    async def get_preset_run(
        self, binding: StudioScope, external_run_id: str
    ) -> Mapping[str, Any]:
        run_id = _opaque_id(external_run_id, "run_", "external_run_id")
        body = await self._json_request(
            "GET", binding, f"/api/v1/external/image-generations/{run_id}"
        )
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        allowed = {
            "run_id", "status", "preset_id", "preset_revision_id",
            "revision_number", "checksum", "output_asset_ids",
            "output_version_ids", "error_code", "result_deep_link",
        }
        if set(body) - allowed:
            raise RuntimeError("generation_studio_invalid_external_status")
        for key in ("output_asset_ids", "output_version_ids"):
            values = body.get(key)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise RuntimeError("generation_studio_invalid_external_status")
        return dict(body)

    async def get_video_catalog(self, binding: StudioScope) -> Mapping[str, Any]:
        """Read the 73 server-owned video profile catalog.

        The profile route is the semantic contract owned by 73.  AoiTalk does
        not query its package registry or infer selectors from local files.
        """

        body = await self._json_request("GET", binding, "/api/v1/video/profiles")
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        return body

    async def submit_image(
        self,
        binding: StudioScope,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        body = await self._json_request(
            "POST",
            binding,
            "/api/v1/image-generations",
            payload=request,
        )
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        return self._project(body)

    @staticmethod
    def _video_output(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        """Project one typed 73 ``VideoOutput`` into the shared receipt shape."""

        asset_id = raw.get("asset_id")
        output_version = raw.get("output_version_id") or raw.get("output_version")
        checksum = raw.get("checksum") or raw.get("sha256")
        mime_type = raw.get("mime_type")
        if not all(isinstance(value, str) and value.strip() for value in (asset_id, output_version, checksum, mime_type)):
            return None
        content_url = raw.get("content_url") or raw.get("deep_link")
        deep_link: str | None = None
        # 73 normally returns a scope-relative asset route.  Relative paths
        # are provider/path internals at the AoiTalk boundary, so retain a
        # deep link only when the server explicitly supplied a safe absolute
        # URL without query/fragment credentials.
        if isinstance(content_url, str) and content_url.strip():
            try:
                deep_link = _validate_safe_url(content_url, "video output URL")
            except MediaOperationsValidationError:
                deep_link = None
        return {
            "asset_id": asset_id,
            "output_version": output_version,
            "sha256": checksum,
            "mime_type": mime_type,
            "width": raw.get("width"),
            "height": raw.get("height"),
            "deep_link": deep_link,
            "provenance_hash": raw.get("provenance_hash")
            or sha256_json(
                {
                    "asset_id": asset_id,
                    "output_version": output_version,
                    "sha256": checksum,
                    "mime_type": mime_type,
                    "width": raw.get("width"),
                    "height": raw.get("height"),
                    "deep_link": deep_link,
                }
            ),
        }

    @classmethod
    def _project_video(cls, body: Mapping[str, Any], *, fallback_run_id: str | None = None) -> dict[str, Any]:
        """Project a 73 VideoJob (or shared Run) without copying provider data."""

        raw_status = body.get("status")
        status = cls._status(raw_status)
        run_id = body.get("run_id") or fallback_run_id
        outputs_raw = body.get("outputs")
        if not isinstance(outputs_raw, Sequence) or isinstance(outputs_raw, (str, bytes)):
            outputs_raw = []
        outputs: list[dict[str, Any]] = []
        for raw in outputs_raw[:20]:
            if isinstance(raw, Mapping):
                projected = cls._video_output(raw)
                if projected is not None:
                    outputs.append(projected)
        if status == "succeeded" and not outputs:
            status = "output_pending"
        output_asset_ids = body.get("output_asset_ids")
        if not isinstance(output_asset_ids, Sequence) or isinstance(output_asset_ids, (str, bytes)):
            output_asset_ids = [item["asset_id"] for item in outputs]
        output_version_ids = body.get("output_version_ids")
        if not isinstance(output_version_ids, Sequence) or isinstance(output_version_ids, (str, bytes)):
            output_version_ids = [item["output_version"] for item in outputs]
        error = body.get("error")
        error_code = body.get("error_code") or body.get("reason_code")
        if error_code is None and isinstance(error, Mapping):
            error_code = error.get("code")
        return {
            "status": status,
            "run_id": run_id,
            "workspace_id": body.get("workspace_id"),
            "project_id": body.get("project_id"),
            "executor_kind": body.get("executor_kind") or "remote_video",
            "progress": body.get("progress"),
            "attempt": body.get("attempt"),
            "cancel_requested": body.get("cancel_requested"),
            "output_asset_ids": list(output_asset_ids)[:20],
            "output_version_ids": list(output_version_ids)[:20],
            "outputs": outputs,
            "adapter_release": body.get("adapter_release") or "73_generation_studio_video_http",
            "started_at": body.get("started_at") or body.get("created_at"),
            "finished_at": body.get("finished_at") or body.get("updated_at"),
            "cost_summary": body.get("cost_summary") or {},
            "error_code": error_code,
            "result_deep_link": body.get("result_deep_link"),
        }

    async def submit_video(
        self,
        binding: StudioScope,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Execute the 73 semantic video plan/preview/apply/run sequence.

        The sequence is intentionally kept inside the adapter.  AoiTalk sees
        one bounded receipt, while 73 retains ownership of workflow and
        provider details.  Any transport/contract error bubbles to the
        durable-intent caller, which marks the attempt uncertain and never
        retries it implicitly.
        """

        selector = request.get("video_model_selection_id") or request.get("model_selection_id")
        if not isinstance(selector, str) or not _VIDEO_MODEL_SELECTION_RE.fullmatch(selector):
            return {"status": "unavailable", "reason_code": "video_selector_unavailable"}
        # Resolve every workflow/version/model binding through the public
        # profile catalog.  The caller supplies only the opaque selector ID;
        # AoiTalk never persists or forwards provider-internal IDs directly.
        workflow_selection = None
        catalog = await self.get_video_catalog(binding)
        profiles = catalog.get("items") if isinstance(catalog, Mapping) else None
        profiles = profiles if isinstance(profiles, Sequence) and not isinstance(profiles, (str, bytes)) else ()
        for profile in profiles:
            if not isinstance(profile, Mapping) or profile.get("workflow_selector_id") != selector:
                continue
            verification = profile.get("verification")
            if verification is not None and str(verification).strip().lower() != "verified":
                # A stale/unverified profile is not a submission capability;
                # catalog discovery may still expose it as unavailable.
                continue
            if profile.get("enabled") is False:
                continue
            def _first_profile_value(*names: str) -> Any:
                for name in names:
                    candidate = profile.get(name)
                    if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                        candidate = next((item for item in candidate if item is not None), None)
                    if candidate is not None:
                        return candidate
                return None

            workflow_selection = {
                "workflow_selector_id": selector,
                "workflow_version_id": _first_profile_value("workflow_version_id"),
                "render_profile_id": _first_profile_value("render_profile_id"),
                "generation_model_id": _first_profile_value(
                    "generation_model_id", "generation_model_ids"
                ),
                "adapter_id": _first_profile_value("adapter_id", "adapter_ids"),
                "pipeline_preset_id": _first_profile_value(
                    "pipeline_preset_id", "pipeline_preset_ids"
                ),
            }
            workflow_selection = {
                key: value
                for key, value in workflow_selection.items()
                if value is not None
            }
            break
        if not isinstance(workflow_selection, Mapping):
            return {"status": "unavailable", "reason_code": "video_selector_unavailable"}
        safe_selection = dict(workflow_selection)
        safe_selection["verified"] = True
        payload = {
            "workspace_id": binding.workspace_id,
            "project_id": binding.project_id,
            "purpose": "video_prompt",
            "mode": "manual",
            "natural_request": None,
            "prompt": request.get("prompt"),
            "version_refs": None,
            "workflow_selection": safe_selection,
            "asset_bindings": list(request.get("asset_bindings") or ()),
            "media_settings": dict(request.get("media_settings") or {}),
            "idempotency_key": request["idempotency_key"],
        }
        plan_body = await self._json_request("POST", binding, "/api/v1/video/plans/manual", payload=payload)
        if plan_body.get("status") == "unavailable" and plan_body.get("reason_code"):
            return plan_body
        plan_id = plan_body.get("video_plan_id")
        plan_checksum = plan_body.get("plan_checksum")
        if not isinstance(plan_id, str) or not isinstance(plan_checksum, str):
            raise RuntimeError("generation_studio_video_invalid_plan")
        preview_key = "video-preview-" + sha256_json({"plan": plan_id, "key": request["idempotency_key"]})[:48]
        preview = await self._json_request(
            "POST",
            binding,
            f"/api/v1/video/plans/{plan_id}/preview",
            payload={"idempotency_key": preview_key, "expected_plan_checksum": plan_checksum},
        )
        preview_id = preview.get("video_preview_id")
        preview_checksum = preview.get("preview_checksum")
        if not isinstance(preview_id, str) or not isinstance(preview_checksum, str):
            raise RuntimeError("generation_studio_video_invalid_preview")
        application = await self._json_request(
            "POST",
            binding,
            f"/api/v1/video/previews/{preview_id}/apply",
            payload={
                "idempotency_key": "video-apply-" + sha256_json({"preview": preview_id, "key": request["idempotency_key"]})[:48],
                "expected_preview_checksum": preview_checksum,
                "confirmed": True,
            },
        )
        application_id = application.get("video_application_id")
        proposal_checksum = application.get("proposal_checksum") or application.get("application_checksum")
        if not isinstance(application_id, str) or not isinstance(proposal_checksum, str):
            raise RuntimeError("generation_studio_video_invalid_application")
        job = await self._json_request(
            "POST",
            binding,
            f"/api/v1/video/applications/{application_id}/run",
            payload={
                "proposal_checksum": proposal_checksum,
                "idempotency_key": request["idempotency_key"],
            },
        )
        if job.get("status") == "unavailable" and job.get("reason_code"):
            return job
        return self._project_video(job)

    async def get_run(
        self,
        binding: StudioScope,
        external_run_id: str,
    ) -> Mapping[str, Any]:
        body = await self._json_request(
            "GET",
            binding,
            f"/api/v1/jobs/{external_run_id}",
        )
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        return self._project(body, fallback_run_id=external_run_id)

    async def reconcile_video(
        self,
        binding: StudioScope,
        external_run_id: str,
    ) -> Mapping[str, Any]:
        # 73's shared Run endpoint is keyed by the stable run_ receipt that
        # AoiTalk persists.  It carries typed video outputs without exposing
        # the provider's internal video_job_id/workflow details.
        body = await self._json_request("GET", binding, f"/api/v1/jobs/{external_run_id}")
        if body.get("status") == "unavailable" and body.get("reason_code"):
            return body
        return self._project_video(body, fallback_run_id=external_run_id)


_WORKSPACE_ID_RE = re.compile(r"^wsp_[A-Za-z0-9_-]{8,160}$")
_PROJECT_ID_RE = re.compile(r"^prj_[A-Za-z0-9_-]{8,160}$")
_RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]{8,160}$")
_ASSET_ID_RE = re.compile(r"^ast_[A-Za-z0-9_-]{8,160}$")
_OUTPUT_VERSION_RE = re.compile(r"^outv_[A-Za-z0-9_-]{8,160}$")
_MODEL_SELECTION_RE = re.compile(r"^(?:ims_|imd_|ien_)[A-Za-z0-9_-]{8,160}$")
_VIDEO_MODEL_SELECTION_RE = re.compile(r"^wsl_[A-Za-z0-9_-]{8,160}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")

_IMAGE_MIME_RE = re.compile(r"^image/[A-Za-z0-9.+-]{1,64}$", re.IGNORECASE)
_VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
        "video/other",
    }
)

_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "graph",
        "password",
        "path",
        "pathname",
        "private_key",
        "raw_provider_response",
        "secret",
        "session",
        "token",
        "workflow_graph",
    }
)

_ALLOWED_REQUEST_KEYS = frozenset(
    {
        "prompt",
        "negative_prompt",
        "seed",
        "model_selection_id",
        "image_model_selection_id",
        "video_model_selection_id",
        "generation_settings",
        "size_preset_id",
        "width",
        "height",
        "accept_metered_generation",
        "aspect_ratio",
        "duration_seconds",
        "frame_count",
        "storyboard",
    }
)

_ALLOWED_ADAPTER_KEYS = frozenset(
    {
        "status",
        "run_id",
        "workspace_id",
        "project_id",
        "executor_kind",
        "progress",
        "attempt",
        "cancel_requested",
        "output_asset_ids",
        "output_version_ids",
        "outputs",
        "adapter_release",
        "started_at",
        "finished_at",
        "cost_summary",
        "error_code",
        "result_deep_link",
        "reason_code",
    }
)

_ALLOWED_OUTPUT_KEYS = frozenset(
    {
        "asset_id",
        "output_version",
        "sha256",
        "mime_type",
        "width",
        "height",
        "deep_link",
        "provenance_hash",
    }
)

_ALLOWED_CATALOG_KEYS = frozenset(
    {
        "kind",
        "model_selection_id",
        "video_model_selection_id",
        "image_model_selection_id",
        "id",
        "display_name",
        "name",
        "label",
        "status",
        "available",
        # 73 video profile fields are used only to resolve a selection into a
        # server-owned submission payload; they are never returned to clients.
        "workflow_selector_id",
        "workflow_version_id",
        "render_profile_id",
        "generation_model_id",
        "adapter_id",
        "pipeline_preset_id",
        "purpose",
        "verification",
        "enabled",
    }
)

_STATUSES = frozenset(
    {
        "draft",
        "queued",
        "claimed",
        "running",
        "output_pending",
        "succeeded",
        "failed",
        "cancelled",
        "quarantined",
        "uncertain",
        "unavailable",
    }
)
_SIZE_PRESETS = frozenset(
    {"normal_square", "normal_landscape", "normal_portrait", "custom"}
)
_VIDEO_RECIPE_TYPES = frozenset({"video"})


def _key_token(value: Any) -> str:
    rendered = str(value).strip().casefold()
    return re.sub(r"[^a-z0-9]+", "_", rendered).strip("_")


def _reject_forbidden_key(key: Any, label: str) -> None:
    token = _key_token(key)
    compact = token.replace("_", "")
    forbidden_compact = {item.replace("_", "") for item in _FORBIDDEN_KEY_TOKENS}
    if token in _FORBIDDEN_KEY_TOKENS or compact in forbidden_compact:
        raise MediaOperationsValidationError(f"{label} contains a forbidden field")


def _safe_json(value: Any, label: str, *, depth: int = 0) -> Any:
    """Copy bounded JSON while rejecting secret/path/provider internals."""

    if depth > 5:
        raise MediaOperationsValidationError(f"{label} is nested too deeply")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            if len(value) > 8000:
                raise MediaOperationsValidationError(f"{label} value is too long")
            if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
                raise MediaOperationsValidationError(f"{label} contains control characters")
        return value
    if isinstance(value, Mapping):
        if len(value) > 50:
            raise MediaOperationsValidationError(f"{label} has too many fields")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _required_text(raw_key, f"{label} key", 100)
            _reject_forbidden_key(key, label)
            result[key] = _safe_json(raw_value, f"{label}.{key}", depth=depth + 1)
        return result

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 100:
            raise MediaOperationsValidationError(f"{label} has too many items")
        return [_safe_json(item, f"{label}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    raise MediaOperationsValidationError(f"{label} must be JSON compatible")


def _opaque_id(value: Any, prefix: str, label: str) -> str:
    # ``outv_`` is the only known prefix with five characters; its 160-byte
    # suffix therefore has a 165-character total bound.  The other 73 IDs
    # have four-character prefixes and top out at 164 characters.
    rendered = _required_text(value, label, len(prefix) + 160)
    pattern = {
        "wsp_": _WORKSPACE_ID_RE,
        "prj_": _PROJECT_ID_RE,
        "run_": _RUN_ID_RE,
        "ast_": _ASSET_ID_RE,
        "outv_": _OUTPUT_VERSION_RE,
    }.get(prefix)
    if pattern is None or not pattern.fullmatch(rendered):
        raise MediaOperationsValidationError(f"{label} must be an opaque {prefix} reference")
    return rendered


def _opaque_optional(value: Any, prefix: str, label: str) -> str | None:
    if value is None:
        return None
    return _opaque_id(value, prefix, label)


def _validate_safe_url(value: Any, label: str, *, origin_only: bool = False) -> str:
    rendered = _required_text(value, label, 2000)
    if any(ord(char) < 0x20 or char.isspace() for char in rendered):
        raise MediaOperationsValidationError(f"{label} contains unsafe characters")
    try:
        parsed = urlsplit(rendered)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise MediaOperationsValidationError(f"{label} must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise MediaOperationsValidationError(f"{label} must not contain userinfo")
        _ = parsed.port
    except MediaOperationsValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(f"{label} is invalid") from exc
    if parsed.query or parsed.fragment:
        raise MediaOperationsValidationError(f"{label} must not contain query or fragment")
    if origin_only and parsed.path not in {"", "/"}:
        raise MediaOperationsValidationError(f"{label} must be an origin URL")
    if not origin_only:
        return _validated_resource_url(rendered)
    return f"{parsed.scheme.casefold()}://{parsed.netloc}"


def _normalize_workspace_status(value: Any) -> str:
    rendered = str(getattr(value, "value", value)).strip().lower()
    if rendered not in {"configured", "unavailable", "verified"}:
        raise MediaOperationsValidationError("status must be configured, unavailable, or verified")
    return rendered


def _normalize_recipe_type(value: Any) -> str:
    rendered = str(getattr(value, "value", value)).strip().lower()
    if rendered not in {"image", "image_set", "comic", "video", "thumbnail"}:
        raise MediaOperationsValidationError("recipe_type is not supported")
    return rendered


def _normalize_opaque_reference(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return None
    rendered = _required_text(value, label, 255)
    if not _OPAQUE_ID_RE.fullmatch(rendered) or "/" in rendered or "\\" in rendered:
        raise MediaOperationsValidationError(f"{label} must be an opaque reference")
    _reject_forbidden_key(rendered, label)
    return rendered


def _normalize_request_spec(
    value: Any,
    *,
    model_selection_default: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MediaOperationsValidationError("request_spec must be an object")
    keys = {str(key) for key in value.keys()}
    unknown = keys - _ALLOWED_REQUEST_KEYS
    if unknown:
        raise MediaOperationsValidationError("request_spec contains unsupported fields")

    prompt = _required_text(value.get("prompt"), "request_spec.prompt", 8000)
    raw_negative_prompt = value.get("negative_prompt")
    negative_prompt = _optional_text(
        "" if raw_negative_prompt is None else raw_negative_prompt,
        "request_spec.negative_prompt",
        8000,
    ) or ""
    canonical_selection = value.get("model_selection_id")
    image_selection = value.get("image_model_selection_id")
    video_selection = value.get("video_model_selection_id")
    aliases = [
        selection
        for selection in (image_selection, video_selection)
        if selection is not None
    ]
    if len(aliases) > 1 and aliases[0] != aliases[1]:
        raise MediaOperationsValidationError("request_spec.model_selection_id aliases must match")
    if canonical_selection is not None and aliases and canonical_selection != aliases[0]:
        raise MediaOperationsValidationError("request_spec.model_selection_id aliases must match")
    model_selection = canonical_selection or (aliases[0] if aliases else None) or model_selection_default
    model_selection = _required_text(model_selection, "request_spec.model_selection_id", 164)
    if not (_MODEL_SELECTION_RE.fullmatch(model_selection) or _VIDEO_MODEL_SELECTION_RE.fullmatch(model_selection)):
        raise MediaOperationsValidationError("request_spec.model_selection_id must be an opaque image/video selection id")
    selector_kind = "video" if _VIDEO_MODEL_SELECTION_RE.fullmatch(model_selection) else "image"
    if kind is not None:
        normalized_kind = str(kind).strip().lower()
        normalized_kind = "video" if normalized_kind == "video" else "image"
        if normalized_kind != selector_kind:
            raise MediaOperationsValidationError("request_spec selection kind does not match recipe kind")
    else:
        normalized_kind = selector_kind

    generation_settings = value.get("generation_settings", {})
    if not isinstance(generation_settings, Mapping):
        raise MediaOperationsValidationError("request_spec.generation_settings must be an object")
    scalar_settings: dict[str, Any] = {}
    for raw_key, raw_value in generation_settings.items():
        key = _required_text(raw_key, "generation_settings key", 100)
        _reject_forbidden_key(key, "generation_settings")
        if isinstance(raw_value, (Mapping, Sequence)) and not isinstance(raw_value, (str, bytes, bytearray)):
            raise MediaOperationsValidationError("generation_settings values must be scalar")
        if not (raw_value is None or isinstance(raw_value, (str, int, float, bool))):
            raise MediaOperationsValidationError("generation_settings values must be scalar")
        scalar_settings[key] = raw_value

    size_preset = value.get("size_preset_id")
    width = value.get("width")
    height = value.get("height")
    if normalized_kind == "image":
        size_preset = str(size_preset or "normal_square").strip().lower()
        if size_preset not in _SIZE_PRESETS:
            raise MediaOperationsValidationError("request_spec.size_preset_id is not supported")
        if size_preset == "custom":
            if isinstance(width, bool) or isinstance(height, bool):
                raise MediaOperationsValidationError("custom width and height must be positive integers")
            try:
                width = int(width)
                height = int(height)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError("custom width and height must be positive integers") from exc
            if not (64 <= width <= 2048 and 64 <= height <= 2048):
                raise MediaOperationsValidationError(
                    "custom width and height must be between 64 and 2048"
                )
            if width % 64 or height % 64:
                raise MediaOperationsValidationError(
                    "custom width and height must be multiples of 64"
                )
        elif width is not None or height is not None:
            raise MediaOperationsValidationError("width and height are allowed only for custom size")
        duration = frame_count = storyboard = None
    else:
        # Video uses 73's typed media settings, not image size presets.  Keep
        # the three planning controls in the semantic plan so the adapter can
        # convert them to duration_ms/fps without exposing provider internals.
        if any(value is not None for value in (size_preset, width, height, value.get("aspect_ratio"))):
            raise MediaOperationsValidationError("image size fields are not valid for video generation")
        size_preset = None
        width = height = None
        duration_raw = value.get("duration_seconds")
        if duration_raw is None:
            duration = None
        elif isinstance(duration_raw, bool):
            raise MediaOperationsValidationError("request_spec.duration_seconds must be a number")
        else:
            try:
                duration = float(duration_raw)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError("request_spec.duration_seconds must be a number") from exc
            if duration <= 0 or duration > 86_400:
                raise MediaOperationsValidationError("request_spec.duration_seconds is out of range")
        frame_raw = value.get("frame_count")
        if frame_raw is None:
            frame_count = None
        elif isinstance(frame_raw, bool):
            raise MediaOperationsValidationError("request_spec.frame_count must be an integer")
        else:
            try:
                frame_count = int(frame_raw)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError("request_spec.frame_count must be an integer") from exc
            if frame_count < 1 or frame_count > 1_000_000:
                raise MediaOperationsValidationError("request_spec.frame_count is out of range")
        storyboard_value = value.get("storyboard")
        storyboard = [] if storyboard_value is None else _safe_json(storyboard_value, "request_spec.storyboard")
        if not isinstance(storyboard, list):
            raise MediaOperationsValidationError("request_spec.storyboard must be a list")

    seed = value.get("seed")
    if seed is not None:
        if isinstance(seed, bool):
            raise MediaOperationsValidationError("request_spec.seed must be an integer")
        try:
            seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("request_spec.seed must be an integer") from exc
        if seed < 0 or seed > 2**63 - 1:
            raise MediaOperationsValidationError("request_spec.seed is out of range")

    accept_metered = value.get("accept_metered_generation", False)
    if not isinstance(accept_metered, bool):
        raise MediaOperationsValidationError("request_spec.accept_metered_generation must be boolean")
    aspect_ratio = _optional_text(value.get("aspect_ratio"), "request_spec.aspect_ratio", 32)
    if aspect_ratio and not re.fullmatch(
        r"[1-9][0-9]{0,2}:[1-9][0-9]{0,2}", aspect_ratio
    ):
        raise MediaOperationsValidationError(
            "request_spec.aspect_ratio must use positive dimensions"
        )

    result = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "model_selection_id": model_selection,
        "generation_settings": scalar_settings,
        "size_preset_id": size_preset,
        "width": width,
        "height": height,
        "accept_metered_generation": accept_metered,
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration,
        "frame_count": frame_count,
        "storyboard": storyboard,
    }
    if normalized_kind == "image":
        # Preserve the pre-WS4 wire alias for existing clients while making
        # the generic key canonical for new image/video consumers.
        result["image_model_selection_id"] = model_selection
        result.pop("duration_seconds", None)
        result.pop("frame_count", None)
        result.pop("storyboard", None)
    else:
        result.pop("image_model_selection_id", None)
    return result


def _normalize_recipe_revision(*, recipe_type: Any, workflow_id: Any = None, model_selection_id: Any = None,
                               identity_pack_id: Any = None, prompt_schema_id: Any = None, prompt_template: Any,
                               negative_requirements: Any = None, reference_asset_ids: Any = None,
                               aspect_ratio: Any = None, width: Any = None, height: Any = None,
                               duration_seconds: Any = None, frame_count: Any = None, storyboard: Any = None,
                               candidate_count: Any = 1, cost_policy: Any = None,
                               provenance_retention_policy: Any = None, human_review_policy: Any = None) -> dict[str, Any]:
    recipe_type_value = _normalize_recipe_type(recipe_type)
    generation_kind = "video" if recipe_type_value in _VIDEO_RECIPE_TYPES else "image"
    prompt = _required_text(prompt_template, "prompt_template", 8000)
    negative = _optional_text(negative_requirements, "negative_requirements", 8000)

    refs = [] if reference_asset_ids is None else reference_asset_ids
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise MediaOperationsValidationError("reference_asset_ids must be a list")
    if len(refs) > 20:
        raise MediaOperationsValidationError("reference_asset_ids may contain at most 20 items")
    reference_ids: list[str] = []
    for raw in refs:
        item = _normalize_opaque_reference(raw, "reference_asset_id", required=True)
        assert item is not None
        if item not in reference_ids:
            reference_ids.append(item)

    def _positive_int(value: Any, label: str, maximum: int) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise MediaOperationsValidationError(f"{label} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(f"{label} must be an integer") from exc
        if parsed < 1 or parsed > maximum:
            raise MediaOperationsValidationError(f"{label} is out of range")
        return parsed

    width_value = _positive_int(width, "width", 16384)
    height_value = _positive_int(height, "height", 16384)
    if duration_seconds is None:
        duration_value = None
    elif isinstance(duration_seconds, bool):
        raise MediaOperationsValidationError("duration_seconds must be a number")
    else:
        try:
            duration_value = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("duration_seconds must be a number") from exc
        if duration_value <= 0 or duration_value > 86400:
            raise MediaOperationsValidationError("duration_seconds is out of range")
    frame_value = _positive_int(frame_count, "frame_count", 1_000_000)
    if isinstance(candidate_count, bool):
        raise MediaOperationsValidationError("candidate_count must be an integer")
    try:
        candidates = int(candidate_count)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError("candidate_count must be an integer") from exc
    if candidates < 1 or candidates > 20:
        raise MediaOperationsValidationError("candidate_count must be between 1 and 20")

    model_ref = None
    if model_selection_id is not None and str(model_selection_id).strip():
        model_ref = _required_text(model_selection_id, "model_selection_id", 164)
        expected_pattern = _VIDEO_MODEL_SELECTION_RE if generation_kind == "video" else _MODEL_SELECTION_RE
        if not expected_pattern.fullmatch(model_ref):
            raise MediaOperationsValidationError(
                "model_selection_id does not match recipe kind"
            )
    elif generation_kind == "video":
        raise MediaOperationsValidationError("video recipe requires a wsl_ model selection")
    if generation_kind == "image" and (
        duration_value is not None
        or frame_value is not None
        or storyboard not in (None, [], "")
    ):
        raise MediaOperationsValidationError("video fields are not valid for image recipes")

    return {
        "recipe_type": recipe_type_value,
        "workflow_id": _normalize_opaque_reference(workflow_id, "workflow_id"),
        "model_selection_id": model_ref,
        "identity_pack_id": _normalize_opaque_reference(identity_pack_id, "identity_pack_id"),
        "prompt_schema_id": _normalize_opaque_reference(prompt_schema_id, "prompt_schema_id"),
        "prompt_template": prompt,
        "negative_requirements": negative,
        "reference_asset_ids": reference_ids,
        "aspect_ratio": _optional_text(aspect_ratio, "aspect_ratio", 32),
        "width": width_value,
        "height": height_value,
        "duration_seconds": duration_value,
        "frame_count": frame_value,
        "storyboard": None if storyboard is None else _safe_json(storyboard, "storyboard"),
        "candidate_count": candidates,
        "cost_policy": {} if cost_policy is None else _safe_json(cost_policy, "cost_policy"),
        "provenance_retention_policy": (
            {} if provenance_retention_policy is None else _safe_json(provenance_retention_policy, "provenance_retention_policy")
        ),
        "human_review_policy": {} if human_review_policy is None else _safe_json(human_review_policy, "human_review_policy"),
    }


def _parse_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    rendered = _required_text(value, label, 128)
    try:
        return datetime.fromisoformat(rendered.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise MediaOperationsValidationError(f"{label} must be an ISO timestamp") from exc


class MediaOperationsGenerationService(MediaOperationsService):
    """WS4 semantic planning, intent fencing, and opaque receipt ledger."""

    def __init__(self, session: Any | None = None, adapter: GenerationStudioAdapter | None = None):
        super().__init__(session=session)
        if adapter is not None:
            self.adapter = adapter
        elif (
            os.getenv("AOITALK_GENERATION_STUDIO_BEARER_TOKEN")
            or os.getenv("GENERATION_STUDIO_BEARER_TOKEN")
        ):
            self.adapter = HttpGenerationStudioAdapter()
        else:
            self.adapter = UnavailableGenerationStudioAdapter()

    async def list_generation_catalog(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        workspace_id: UUID | str | None = None,
        kind: str = "image",
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Return a bounded capability catalog without provider internals.

        Catalog discovery is intentionally conservative: the default adapter
        reports unavailable and no model/workflow inventory.  A configured
        adapter may expose a small semantic catalog method, but arbitrary
        provider payloads are never copied into the API response.
        """

        # The WS4 route now calls this generic spelling for both image and
        # video.  Delegate to the workspace-scoped adapter seam whenever the
        # caller supplies a workspace; retaining the legacy no-argument
        # projection keeps older health checks backwards compatible.
        if workspace_id is not None:
            return await self.list_image_catalog(
                session,
                actor,
                workspace_id=workspace_id,
                kind=kind,
            )
        del actor  # catalog metadata is not user-specific
        # Catalog metadata is safe and static; unlike entity reads it does not
        # require a database session when used by health/availability checks.
        if session is not None:
            self._resolve_session(session)
        catalog = {
            "provider": "comfyui_workbench",
            "status": "unavailable",
            "image_model_selection_ids": [],
            "size_preset_ids": [
                "normal_square",
                "normal_landscape",
                "normal_portrait",
                "custom",
            ],
        }
        discover = getattr(self.adapter, "get_catalog", None)
        if not callable(discover):
            return catalog
        try:
            raw = discover()
            raw = await raw if inspect.isawaitable(raw) else raw
            if not isinstance(raw, Mapping):
                return catalog
            ids = raw.get("image_model_selection_ids", [])
            if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
                return catalog
            normalized_ids = [
                value
                for value in (_required_text(item, "image_model_selection_id", 128) for item in ids)
                if _MODEL_SELECTION_RE.fullmatch(value)
            ][:100]
            status = str(raw.get("status") or "verified").strip().lower()
            if status not in {"configured", "verified", "unavailable"}:
                status = "verified"
            catalog.update({"status": status, "image_model_selection_ids": normalized_ids})
        except Exception:
            # Discovery failure is not a reason to surface provider details.
            return catalog
        return catalog

    async def list_image_catalog(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        workspace_id: UUID | str | None = None,
        kind: str = "image",
    ) -> list[dict[str, Any]]:
        """Return the bounded image/video model-selection catalog.

        This is the 73 ``image-generation/catalog`` or ``video/profiles`` seam.
        It is deliberately *not* an output or asset catalog: only opaque
        model-selection IDs and safe display/availability fields cross into
        AoiTalk.
        """

        session = self._resolve_session(session)
        if actor is None or workspace_id is None:
            raise MediaOperationsValidationError("actor and workspace_id are required")
        kind_value = str(kind).strip().lower()
        if kind_value not in {"image", "video"}:
            raise MediaOperationsValidationError("kind must be image or video")
        workspace = await self._get_row(session, GenerationWorkspace, workspace_id, "workspace_id")
        await self._assert_entity_access(session, actor, workspace, permission="read")
        workspace_status = str(getattr(workspace, "status", "configured") or "configured").strip().lower()
        if workspace_status not in {"configured", "verified"}:
            # A disabled/unavailable workspace must fail closed; do not query
            # the adapter or expose stale capability metadata.
            return []
        discover = getattr(
            self.adapter,
            "get_video_catalog" if kind_value == "video" else "get_image_catalog",
            None,
        )
        if not callable(discover):
            discover = getattr(
                self.adapter,
                "list_video_catalog" if kind_value == "video" else "list_image_catalog",
                None,
            )
        if not callable(discover):
            discover = getattr(self.adapter, "get_catalog", None)
        if not callable(discover):
            return []
        try:
            scope = StudioScope(
                workspace.external_workspace_id,
                workspace.external_project_id,
                workspace.base_url,
            )
            raw = self._call_catalog_adapter(discover, scope)
            raw = await raw if inspect.isawaitable(raw) else raw
        except Exception:
            return []

        if isinstance(raw, Mapping):
            if str(raw.get("status") or "").strip().lower() in {"unavailable", "unverified"}:
                return []
            items: Any = raw.get("items")
            if items is None:
                # 73 Generation Studio exposes its public catalog as a
                # flattened ``selections`` array.  Keep that array bounded and
                # discard provider-only fields before strict projection.
                items = raw.get("selections")
                if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                    items = [
                        {
                            key: item.get(key)
                            for key in _ALLOWED_CATALOG_KEYS
                            if isinstance(item, Mapping) and key in item
                        }
                        for item in items
                    ]
            if items is None:
                items = raw.get(
                    "video_profiles" if kind_value == "video" else "image_model_selections"
                )
            if items is None:
                items = raw.get("models")
            selection_key = (
                "video_model_selection_ids"
                if kind_value == "video"
                else "image_model_selection_ids"
            )
            if items is None and isinstance(raw.get(selection_key), Sequence):
                items = [
                    {"model_selection_id": item}
                    for item in raw[selection_key]
                ]
        else:
            items = raw
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            return []

        result: list[dict[str, Any]] = []
        for item in items[:100]:
            if isinstance(item, str):
                item = {"model_selection_id": item}
            if not isinstance(item, Mapping):
                continue
            try:
                keys = {str(key) for key in item}
                if keys - _ALLOWED_CATALOG_KEYS:
                    raise MediaOperationsValidationError(
                        "Generation Studio catalog contains unsupported fields"
                    )
                selection_id = item.get(
                    "model_selection_id",
                    item.get(
                        "video_model_selection_id" if kind_value == "video" else "image_model_selection_id",
                        item.get("workflow_selector_id", item.get("id")),
                    ),
                )
                selection_id = _required_text(
                    selection_id, "model_selection_id", 164
                )
                expected_pattern = _VIDEO_MODEL_SELECTION_RE if kind_value == "video" else _MODEL_SELECTION_RE
                if not expected_pattern.fullmatch(selection_id):
                    raise MediaOperationsValidationError(
                        "catalog model_selection_id is invalid"
                    )
                display_name = item.get("display_name")
                if display_name is None:
                    display_name = item.get("name", item.get("label"))
                if display_name is not None:
                    display_name = _required_text(display_name, "catalog.display_name", 255)
                raw_status = item.get("status")
                if raw_status is None and "available" in item:
                    raw_status = "available" if item.get("available") else "unavailable"
                status = (
                    str(raw_status).strip().lower()
                    if raw_status is not None
                    else "available"
                )
                if status not in {"available", "unavailable", "configured", "verified"}:
                    status = "unavailable"
                normalized = {
                    "kind": kind_value,
                    "model_selection_id": selection_id,
                    "image_model_selection_id": selection_id if kind_value == "image" else None,
                    "display_name": display_name,
                    "status": status,
                }
            except (MediaOperationsValidationError, TypeError, ValueError):
                continue
            result.append(normalized)
        return result

    async def list_video_catalog(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        workspace_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """Canonical video alias for callers that do not pass ``kind``."""

        return await self.list_image_catalog(
            session,
            actor,
            workspace_id=workspace_id,
            kind="video",
        )

    @staticmethod
    def _call_catalog_adapter(method: Any, scope: StudioScope) -> Any:
        """Call catalog adapters without retrying a provider request on TypeError."""

        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        names = {parameter.name for parameter in parameters}
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        if "binding" in names or "scope" in names or positional:
            return method(scope)
        return method()

    async def _get_row(self, session: Any, model: Any, entity_id: UUID | str, label: str, *, for_update: bool = False) -> Any:
        parsed = _as_uuid(entity_id, label)
        assert parsed is not None
        statement = select(model).where(model.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()
        row = await self._scalar(session, statement)
        if row is None:
            raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _find_scoped_idempotency(self, session: Any, model: Any, *, owner_user_id: UUID, project_id: UUID | None, idempotency_key: str) -> Any:
        conditions = [model.idempotency_key == idempotency_key]
        if project_id is None:
            conditions.extend([model.project_id.is_(None), model.owner_user_id == owner_user_id])
        else:
            conditions.append(model.project_id == project_id)
        return await self._scalar(session, select(model).where(*conditions).limit(1))

    async def _latest_revision(self, session: Any, recipe_id: UUID) -> CreativeRecipeRevision | None:
        return await self._scalar(
            session,
            select(CreativeRecipeRevision)
            .where(CreativeRecipeRevision.creative_recipe_id == recipe_id)
            .order_by(CreativeRecipeRevision.version.desc(), CreativeRecipeRevision.id.desc())
            .limit(1),
        )

    @staticmethod
    def _human_or_admin(actor: Any) -> bool:
        """Return whether the caller is an authenticated human command.

        ``actor_type`` is server-projected by the route layer.  In particular,
        a client-supplied ``actor_type=human`` marker is discarded before this
        service sees it.  An explicit admin role is accepted only when its
        projected type is human/admin; agent/system callers remain denied.
        """

        actor_type = str(
            actor.get("actor_type", "") if isinstance(actor, Mapping) else getattr(actor, "actor_type", "")
        ).strip().lower()
        role = str(
            actor.get("role", "") if isinstance(actor, Mapping) else getattr(actor, "role", "")
        ).strip().lower()
        if actor_type in {"agent", "system"}:
            return False
        # The route's authentication projection intentionally preserves a
        # trusted admin role even when its actor_type is ``unknown`` for a
        # non-browser bearer principal.  Treat that server-owned role as the
        # admin authority; callers marked agent/system remain denied above.
        if role == "admin":
            return True
        if actor_type == "admin":
            return role == "admin"
        return actor_type == "human" and (role in {"", "user", "admin"})

    async def _assert_content_graph(
        self,
        session: Any,
        actor: Any,
        persona_revision: PersonaRevision,
        content_item_id: UUID | None,
        content_variant_id: UUID | None,
    ) -> None:
        """Validate the full Persona → ContentItem → Variant owner graph."""

        if content_variant_id is not None and content_item_id is None:
            raise MediaOperationsValidationError(
                "content_variant_id requires content_item_id"
            )
        content_item = None
        if content_item_id is not None:
            content_item = await self._get_row(
                session, ContentItem, content_item_id, "content_item_id"
            )
            await self._assert_entity_access(session, actor, content_item, permission="read")
            if content_item.project_id != persona_revision.project_id:
                raise MediaOperationsValidationError(
                    "content_item must share Persona project scope"
                )
            # Legacy ContentItems may not carry a PersonaRevision.  When the
            # binding exists, it must be the exact pinned Character revision;
            # accepting a different character here would create a cross-owner
            # generation receipt.
            if (
                content_item.persona_revision_id is not None
                and content_item.persona_revision_id != persona_revision.id
            ):
                raise MediaOperationsValidationError(
                    "content_item must belong to the pinned Persona revision"
                )
        if content_variant_id is None:
            return
        variant = await self._get_row(
            session, ContentVariant, content_variant_id, "content_variant_id"
        )
        await self._assert_entity_access(session, actor, variant, permission="read")
        if (
            variant.content_item_id != content_item_id
            or variant.project_id != persona_revision.project_id
        ):
            raise MediaOperationsValidationError(
                "content_variant must belong to the bound ContentItem and scope"
            )
        # A variant revision is the authoritative character pin when present.
        latest_variant_revision = await self._scalar(
            session,
            select(ContentVariantRevision)
            .where(ContentVariantRevision.content_variant_id == variant.id)
            .order_by(
                ContentVariantRevision.version.desc(),
                ContentVariantRevision.id.desc(),
            )
            .limit(1),
        )
        if latest_variant_revision is not None:
            if (
                latest_variant_revision.persona_revision_id != persona_revision.id
                or latest_variant_revision.content_item_id != content_item_id
                or latest_variant_revision.project_id != persona_revision.project_id
            ):
                raise MediaOperationsValidationError(
                    "content_variant revision does not belong to the pinned Persona graph"
                )

    async def _visible(self, session: Any, actor: Any, model: Any, *, project_id: UUID | None = None, limit: Any = 100, offset: Any = 0, extra: Sequence[Any] = ()) -> list[Any]:
        page_limit, page_offset = _bounded_page(limit, offset)
        rows = await self._scalars(
            session,
            select(model).where(*extra).order_by(model.created_at.desc(), model.id.desc()),
        )
        visible: list[Any] = []
        for row in rows:
            if project_id is not None and getattr(row, "project_id", None) != project_id:
                continue
            try:
                await self._assert_entity_access(session, actor, row, permission="read")
            except Exception:
                continue
            visible.append(row)
        return visible[page_offset : page_offset + page_limit]

    async def create_generation_workspace(self, session: Any | None = None, actor: Any | None = None, *, project_id: UUID | str | None = None, provider: Any = "comfyui_workbench", external_workspace_id: Any, external_project_id: Any = None, base_url: Any, status: Any = "configured", idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        actor_id = await self._assert_create_scope(session, actor, project_uuid)
        provider_value = _required_text(provider, "provider", 64).lower()
        if provider_value != "comfyui_workbench":
            raise MediaOperationsValidationError("provider must be comfyui_workbench")
        workspace_ref = _opaque_id(external_workspace_id, "wsp_", "external_workspace_id")
        project_ref = _opaque_optional(external_project_id, "prj_", "external_project_id")
        safe_url = _validate_safe_url(base_url, "base_url", origin_only=True)
        status_value = _normalize_workspace_status(status)
        key = _idempotency_key(idempotency_key)
        config = {"provider": provider_value, "external_workspace_id": workspace_ref, "external_project_id": project_ref, "base_url": safe_url, "status": status_value}
        config_hash = sha256_json(config)

        existing = await self._find_scoped_idempotency(session, GenerationWorkspace, owner_user_id=actor_id, project_id=project_uuid, idempotency_key=key)
        if existing is not None:
            if existing.config_hash != config_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different workspace payload")
            return existing.to_safe_dict()

        workspace = GenerationWorkspace(
            id=uuid4(), owner_user_id=actor_id, project_id=project_uuid, provider=provider_value,
            external_workspace_id=workspace_ref, external_project_id=project_ref, base_url=safe_url,
            status=status_value, config_hash=config_hash, idempotency_key=key, created_by=actor_id,
        )
        session.add(workspace)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, GenerationWorkspace, owner_user_id=actor_id, project_id=project_uuid, idempotency_key=key)
            if recovered is not None and recovered.config_hash == config_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("workspace conflicts with an existing record") from exc
        return workspace.to_safe_dict()

    async def list_generation_workspaces(self, session: Any | None = None, actor: Any | None = None, *, project_id: UUID | str | None = None, limit: Any = 100, offset: Any = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        rows = await self._visible(session, actor, GenerationWorkspace, project_id=project_uuid, limit=limit, offset=offset)
        return [row.to_safe_dict() for row in rows]

    async def get_generation_workspace(self, session: Any | None = None, actor: Any | None = None, workspace_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or workspace_id is None:
            raise MediaOperationsValidationError("actor and workspace_id are required")
        row = await self._get_row(session, GenerationWorkspace, workspace_id, "workspace_id")
        await self._assert_entity_access(session, actor, row, permission="read")
        return row.to_safe_dict()

    async def _recipe_detail(self, session: Any, recipe: CreativeRecipe) -> dict[str, Any]:
        revisions = await self._scalars(
            session,
            select(CreativeRecipeRevision)
            .where(CreativeRecipeRevision.creative_recipe_id == recipe.id)
            .order_by(CreativeRecipeRevision.version.desc(), CreativeRecipeRevision.id.desc())
            .limit(101),
        )
        if not revisions:
            raise MediaOperationsConflictError("CreativeRecipe revision history is incomplete")
        return {**recipe.to_safe_dict(), "current_revision": revisions[0].to_safe_dict(), "revisions": [item.to_safe_dict() for item in revisions[:100]], "revision_history_truncated": len(revisions) > 100}

    async def create_creative_recipe(self, session: Any | None = None, actor: Any | None = None, *, persona_id: UUID | str, persona_revision_id: UUID | str | None = None, project_id: UUID | str | None = None, name: Any = None, recipe_type: Any, workflow_id: Any = None, model_selection_id: Any = None, image_model_selection_id: Any = None, identity_pack_id: Any = None, prompt_schema_id: Any = None, prompt_template: Any, negative_requirements: Any = None, reference_asset_ids: Any = None, aspect_ratio: Any = None, width: Any = None, height: Any = None, duration_seconds: Any = None, frame_count: Any = None, storyboard: Any = None, candidate_count: Any = 1, cost_policy: Any = None, provenance_retention_policy: Any = None, human_review_policy: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        persona = await self._get_row(session, Persona, persona_id, "persona_id")
        actor_id = await self._assert_entity_access(session, actor, persona, permission="write")
        if project_id is not None:
            parsed_project_id = _as_uuid(project_id, "project_id", required=False)
            if parsed_project_id != persona.project_id:
                raise MediaOperationsValidationError("project_id must match Persona scope")
        recipe_name = _required_text(name or "Untitled recipe", "name", 255)
        if model_selection_id is None:
            model_selection_id = image_model_selection_id
        content = _normalize_recipe_revision(
            recipe_type=recipe_type, workflow_id=workflow_id, model_selection_id=model_selection_id,
            identity_pack_id=identity_pack_id, prompt_schema_id=prompt_schema_id, prompt_template=prompt_template,
            negative_requirements=negative_requirements, reference_asset_ids=reference_asset_ids, aspect_ratio=aspect_ratio,
            width=width, height=height, duration_seconds=duration_seconds, frame_count=frame_count, storyboard=storyboard,
            candidate_count=candidate_count, cost_policy=cost_policy, provenance_retention_policy=provenance_retention_policy,
            human_review_policy=human_review_policy,
        )
        parsed_revision_id = _as_uuid(persona_revision_id, "persona_revision_id", required=False)
        if parsed_revision_id is None:
            parsed_revision = await self._scalar(
                session,
                select(PersonaRevision).where(PersonaRevision.persona_id == persona.id).order_by(PersonaRevision.version.desc()).limit(1),
            )
        else:
            parsed_revision = await self._get_row(session, PersonaRevision, parsed_revision_id, "persona_revision_id")
        if parsed_revision is None or parsed_revision.persona_id != persona.id or parsed_revision.project_id != persona.project_id:
            raise MediaOperationsValidationError("persona_revision_id must belong to the Persona and scope")
        parsed_revision_id = parsed_revision.id
        create_hash = sha256_json({"persona_id": str(persona.id), "persona_revision_id": str(parsed_revision_id), "name": recipe_name, "revision": content})
        key = _idempotency_key(idempotency_key)
        existing = await self._find_scoped_idempotency(session, CreativeRecipe, owner_user_id=actor_id, project_id=persona.project_id, idempotency_key=key)
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different CreativeRecipe payload")
            return await self._recipe_detail(session, existing)
        recipe = CreativeRecipe(id=uuid4(), owner_user_id=actor_id, project_id=persona.project_id, persona_id=persona.id, name=recipe_name, create_hash=create_hash, idempotency_key=key, created_by=actor_id)
        revision = self._build_recipe_revision(recipe, parsed_revision_id, 1, content, actor_id, None)
        session.add(recipe)
        session.add(revision)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, CreativeRecipe, owner_user_id=actor_id, project_id=persona.project_id, idempotency_key=key)
            if recovered is not None and recovered.create_hash == create_hash:
                return await self._recipe_detail(session, recovered)
            raise MediaOperationsConflictError("CreativeRecipe conflicts with an existing record") from exc
        return await self._recipe_detail(session, recipe)

    def _build_recipe_revision(self, recipe: CreativeRecipe, persona_revision_id: UUID, version: int, content: Mapping[str, Any], actor_id: UUID, idempotency_key: str | None) -> CreativeRecipeRevision:
        return CreativeRecipeRevision(
            id=uuid4(), creative_recipe_id=recipe.id, owner_user_id=recipe.owner_user_id, project_id=recipe.project_id,
            persona_id=recipe.persona_id, persona_revision_id=persona_revision_id, version=version,
            recipe_type=content["recipe_type"], workflow_id=content["workflow_id"], model_selection_id=content["model_selection_id"],
            identity_pack_id=content["identity_pack_id"], prompt_schema_id=content["prompt_schema_id"], prompt_template=content["prompt_template"],
            negative_requirements=content["negative_requirements"], reference_asset_ids=content["reference_asset_ids"], aspect_ratio=content["aspect_ratio"],
            width=content["width"], height=content["height"], duration_seconds=content["duration_seconds"], frame_count=content["frame_count"],
            storyboard_json=content["storyboard"], candidate_count=content["candidate_count"], cost_policy=content["cost_policy"],
            provenance_retention_policy=content["provenance_retention_policy"], human_review_policy=content["human_review_policy"],
            content_hash=sha256_json({"persona_revision_id": str(persona_revision_id), **dict(content)}), idempotency_key=idempotency_key, created_by=actor_id,
        )

    async def list_creative_recipes(self, session: Any | None = None, actor: Any | None = None, *, persona_id: UUID | str | None = None, project_id: UUID | str | None = None, limit: Any = 100, offset: Any = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        extra: list[Any] = []
        if persona_id is not None:
            persona_uuid = _as_uuid(persona_id, "persona_id")
            extra.append(CreativeRecipe.persona_id == persona_uuid)
        rows = await self._visible(session, actor, CreativeRecipe, project_id=project_uuid, limit=limit, offset=offset, extra=extra)
        return [await self._recipe_detail(session, row) for row in rows]

    async def get_creative_recipe(self, session: Any | None = None, actor: Any | None = None, recipe_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or recipe_id is None:
            raise MediaOperationsValidationError("actor and recipe_id are required")
        recipe = await self._get_row(session, CreativeRecipe, recipe_id, "recipe_id")
        await self._assert_entity_access(session, actor, recipe, permission="read")
        return await self._recipe_detail(session, recipe)

    async def append_creative_recipe_revision(self, session: Any | None = None, actor: Any | None = None, recipe_id: UUID | str | None = None, *, expected_version: Any, persona_revision_id: UUID | str | None = None, recipe_type: Any, workflow_id: Any = None, model_selection_id: Any = None, image_model_selection_id: Any = None, identity_pack_id: Any = None, prompt_schema_id: Any = None, prompt_template: Any, negative_requirements: Any = None, reference_asset_ids: Any = None, aspect_ratio: Any = None, width: Any = None, height: Any = None, duration_seconds: Any = None, frame_count: Any = None, storyboard: Any = None, candidate_count: Any = 1, cost_policy: Any = None, provenance_retention_policy: Any = None, human_review_policy: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or recipe_id is None:
            raise MediaOperationsValidationError("actor and recipe_id are required")
        recipe = await self._get_row(session, CreativeRecipe, recipe_id, "recipe_id", for_update=True)
        actor_id = await self._assert_entity_access(session, actor, recipe, permission="write")
        if model_selection_id is None:
            model_selection_id = image_model_selection_id
        current = await self._latest_revision(session, recipe.id)
        if current is None:
            raise MediaOperationsConflictError("CreativeRecipe revision history is incomplete")
        content = _normalize_recipe_revision(
            recipe_type=recipe_type, workflow_id=workflow_id, model_selection_id=model_selection_id,
            identity_pack_id=identity_pack_id, prompt_schema_id=prompt_schema_id, prompt_template=prompt_template,
            negative_requirements=negative_requirements, reference_asset_ids=reference_asset_ids, aspect_ratio=aspect_ratio,
            width=width, height=height, duration_seconds=duration_seconds, frame_count=frame_count, storyboard=storyboard,
            candidate_count=candidate_count, cost_policy=cost_policy, provenance_retention_policy=provenance_retention_policy,
            human_review_policy=human_review_policy,
        )
        key = _idempotency_key(idempotency_key)
        persona_revision = current.persona_revision_id
        if persona_revision_id is not None:
            persona_revision = _as_uuid(persona_revision_id, "persona_revision_id")
            assert persona_revision is not None
            row = await self._get_row(session, PersonaRevision, persona_revision, "persona_revision_id")
            if row.persona_id != recipe.persona_id or row.project_id != recipe.project_id:
                raise MediaOperationsValidationError("persona_revision_id must belong to the recipe Persona and scope")
        content_hash = sha256_json({"persona_revision_id": str(persona_revision), **dict(content)})
        existing = await self._scalar(session, select(CreativeRecipeRevision).where(CreativeRecipeRevision.creative_recipe_id == recipe.id, CreativeRecipeRevision.idempotency_key == key).limit(1))
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different revision content")
            return existing.to_safe_dict()
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("expected_version must be an integer") from exc
        if expected != int(current.version):
            raise MediaOperationsConflictError("stale CreativeRecipe version")
        revision = self._build_recipe_revision(recipe, persona_revision, expected + 1, content, actor_id, key)
        session.add(revision)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(session, select(CreativeRecipeRevision).where(CreativeRecipeRevision.creative_recipe_id == recipe.id, CreativeRecipeRevision.idempotency_key == key).limit(1))
            if recovered is not None and recovered.content_hash == content_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("CreativeRecipe revision changed concurrently") from exc
        return revision.to_safe_dict()

    async def create_generation_plan(self, session: Any | None = None, actor: Any | None = None, *, persona_revision_id: UUID | str, creative_recipe_revision_id: UUID | str, workspace_id: UUID | str, requested_outputs: Any = 1, request_spec: Any, content_item_id: UUID | str | None = None, content_variant_id: UUID | str | None = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        persona_revision = await self._get_row(session, PersonaRevision, persona_revision_id, "persona_revision_id")
        actor_id = await self._assert_entity_access(session, actor, persona_revision, permission="write")
        recipe_revision = await self._get_row(session, CreativeRecipeRevision, creative_recipe_revision_id, "creative_recipe_revision_id")
        workspace = await self._get_row(session, GenerationWorkspace, workspace_id, "workspace_id")
        await self._assert_entity_access(session, actor, recipe_revision, permission="read")
        await self._assert_entity_access(session, actor, workspace, permission="read")
        if recipe_revision.project_id != persona_revision.project_id or recipe_revision.persona_id != persona_revision.persona_id:
            raise MediaOperationsValidationError("PersonaRevision and CreativeRecipeRevision must share Persona scope")
        if workspace.project_id != persona_revision.project_id:
            raise MediaOperationsValidationError("GenerationWorkspace must share Persona project scope")
        if isinstance(requested_outputs, bool):
            raise MediaOperationsValidationError("requested_outputs must be an integer")
        try:
            outputs_count = int(requested_outputs)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("requested_outputs must be an integer") from exc
        if outputs_count < 1 or outputs_count > 20:
            raise MediaOperationsValidationError("requested_outputs must be between 1 and 20")
        parsed_content_item_id = _as_uuid(content_item_id, "content_item_id", required=False)
        parsed_variant_id = _as_uuid(content_variant_id, "content_variant_id", required=False)
        await self._assert_content_graph(
            session,
            actor,
            persona_revision,
            parsed_content_item_id,
            parsed_variant_id,
        )
        normalized_spec = _normalize_request_spec(
            request_spec,
            model_selection_default=recipe_revision.model_selection_id,
            kind=recipe_revision.recipe_type,
        )
        key = _idempotency_key(idempotency_key)
        plan_hash = sha256_json({
            "persona_revision_id": str(persona_revision.id), "persona_revision_hash": persona_revision.content_hash,
            "creative_recipe_revision_id": str(recipe_revision.id), "workspace_id": str(workspace.id),
            "requested_outputs": outputs_count, "request_spec": normalized_spec,
            "content_item_id": str(parsed_content_item_id) if parsed_content_item_id else None,
            "content_variant_id": str(parsed_variant_id) if parsed_variant_id else None,
        })
        existing = await self._find_scoped_idempotency(session, GenerationPlan, owner_user_id=actor_id, project_id=persona_revision.project_id, idempotency_key=key)
        if existing is not None:
            if existing.plan_hash != plan_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different GenerationPlan payload")
            return await self._plan_detail(session, existing)
        plan = GenerationPlan(
            id=uuid4(), owner_user_id=actor_id, project_id=persona_revision.project_id, persona_revision_id=persona_revision.id,
            persona_revision_hash=persona_revision.content_hash, content_item_id=parsed_content_item_id, content_variant_id=parsed_variant_id,
            creative_recipe_revision_id=recipe_revision.id, workspace_id=workspace.id, requested_outputs=outputs_count,
            request_spec=normalized_spec, idempotency_key=key, plan_hash=plan_hash, status="draft", created_by=actor_id,
        )
        session.add(plan)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, GenerationPlan, owner_user_id=actor_id, project_id=persona_revision.project_id, idempotency_key=key)
            if recovered is not None and recovered.plan_hash == plan_hash:
                return await self._plan_detail(session, recovered)
            raise MediaOperationsConflictError("GenerationPlan conflicts with an existing record") from exc
        return await self._plan_detail(session, plan)

    async def list_generation_plans(self, session: Any | None = None, actor: Any | None = None, *, project_id: UUID | str | None = None, workspace_id: UUID | str | None = None, limit: Any = 100, offset: Any = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        extra: list[Any] = []
        if workspace_id is not None:
            workspace_uuid = _as_uuid(workspace_id, "workspace_id")
            extra.append(GenerationPlan.workspace_id == workspace_uuid)
        rows = await self._visible(session, actor, GenerationPlan, project_id=project_uuid, limit=limit, offset=offset, extra=extra)
        return [await self._plan_detail(session, row) for row in rows]

    async def _plan_detail(self, session: Any, plan: GenerationPlan) -> dict[str, Any]:
        intent = await self._scalar(session, select(GenerationRunIntent).where(GenerationRunIntent.plan_id == plan.id).limit(1))
        run = await self._scalar(session, select(GenerationRun).where(GenerationRun.plan_id == plan.id).limit(1))
        recipe_revision = await self._scalar(
            session,
            select(CreativeRecipeRevision)
            .where(CreativeRecipeRevision.id == plan.creative_recipe_revision_id)
            .limit(1),
        )
        selector = (plan.request_spec or {}).get("model_selection_id") or (
            plan.request_spec or {}
        ).get("image_model_selection_id")
        generation_kind = (
            "video"
            if recipe_revision is not None and recipe_revision.recipe_type == "video"
            else "video"
            if isinstance(selector, str) and selector.startswith("wsl_")
            else "image"
        )
        result = {
            **plan.to_safe_dict(),
            "generation_kind": generation_kind,
            "intent": intent.to_safe_dict() if intent else None,
            "run": await self._run_detail(session, run) if run else None,
        }
        # ``request_spec`` predates the generic selector key.  Normalize the
        # safe projection so the API has one canonical key while preserving the
        # image alias for rolling clients.
        spec = dict(result.get("request_spec") or {})
        if selector is not None:
            spec["model_selection_id"] = selector
            if generation_kind == "image":
                spec["image_model_selection_id"] = selector
            else:
                spec.pop("image_model_selection_id", None)
        result["request_spec"] = spec
        return result

    @staticmethod
    def _intent_request_hash(
        plan: GenerationPlan,
        recipe_revision: CreativeRecipeRevision,
        generation_kind: str,
        *,
        acknowledge_metered_generation: bool,
    ) -> str:
        """Hash the exact semantic request fenced by the durable intent.

        ``GenerationPlan.plan_hash`` alone is insufficient to distinguish an
        image/video dispatch or an explicit billing acknowledgement during a
        rolling deployment.  Keep the hash deterministic and provider-free:
        plan identity/hash, derived kind, normalized request spec, and the
        server-validated acknowledgement are the complete boundary.
        """

        del recipe_revision  # kind is already derived from the pinned revision
        return sha256_json(
            {
                "plan_id": str(plan.id),
                "plan_hash": plan.plan_hash,
                "generation_kind": generation_kind,
                "request_spec": dict(plan.request_spec or {}),
                "acknowledge_metered_generation": bool(
                    acknowledge_metered_generation
                ),
            }
        )

    async def get_generation_plan(self, session: Any | None = None, actor: Any | None = None, plan_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or plan_id is None:
            raise MediaOperationsValidationError("actor and plan_id are required")
        plan = await self._get_row(session, GenerationPlan, plan_id, "plan_id")
        await self._assert_entity_access(session, actor, plan, permission="read")
        return await self._plan_detail(session, plan)

    def _adapter_request(
        self,
        plan: GenerationPlan,
        recipe_revision: CreativeRecipeRevision,
        external_idempotency_key: str,
        *,
        acknowledge_metered_generation: bool = False,
    ) -> dict[str, Any]:
        spec = dict(plan.request_spec or {})
        kind = "video" if recipe_revision.recipe_type == "video" else "image"
        if kind == "video":
            settings = dict(spec.get("generation_settings") or {})
            duration_seconds = spec.get("duration_seconds")
            frame_count = spec.get("frame_count")
            if duration_seconds is None:
                duration_seconds = recipe_revision.duration_seconds
            if frame_count is None:
                frame_count = recipe_revision.frame_count
            if duration_seconds is None:
                raise MediaOperationsValidationError(
                    "video generation requires duration_seconds"
                )
            # 73's typed VideoMediaSettings uses milliseconds/fps.  ``fps`` is
            # an optional scalar generation control; keep the default bounded.
            fps = settings.pop("fps", 24)
            if isinstance(fps, bool):
                raise MediaOperationsValidationError("video fps must be a number")
            try:
                fps = float(fps)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError("video fps must be a number") from exc
            if not 1 <= fps <= 240:
                raise MediaOperationsValidationError("video fps is out of range")
            media_settings: dict[str, Any] = {
                "duration_ms": int(round(float(duration_seconds) * 1000)),
                "fps": fps,
            }
            if frame_count is not None:
                media_settings["frame_count"] = frame_count
            storyboard = spec.get("storyboard")
            if storyboard:
                media_settings["storyboard"] = storyboard
            # Only the typed 73 media settings are forwarded.  Arbitrary
            # generation controls stay in AoiTalk's hash and are not treated
            # as provider instructions.
            for setting_key in ("width", "height", "codec", "audio", "poster"):
                if setting_key in settings:
                    media_settings[setting_key] = settings[setting_key]
            video_request = {
                "prompt": spec["prompt"],
                "negative_prompt": spec.get("negative_prompt"),
                "model_selection_id": spec["model_selection_id"],
                "video_model_selection_id": spec["model_selection_id"],
                "generation_settings": dict(spec.get("generation_settings") or {}),
                "duration_seconds": duration_seconds,
                "frame_count": frame_count,
                "storyboard": storyboard or [],
                "media_settings": media_settings,
                "accept_metered_generation": bool(spec.get("accept_metered_generation", False)),
                "idempotency_key": external_idempotency_key,
            }
            if acknowledge_metered_generation:
                video_request["acknowledge_metered_generation"] = True
            return video_request
        # The persisted plan has already been normalized; copy only the exact
        # 73 image submit contract and attach deterministic idempotency.
        image_request = {
            "prompt": spec["prompt"],
            "negative_prompt": spec.get("negative_prompt"),
            "seed": spec.get("seed"),
            "image_model_selection_id": spec["model_selection_id"],
            "generation_settings": dict(spec.get("generation_settings") or {}),
            "size_preset_id": spec.get("size_preset_id", "normal_square"),
            "width": spec.get("width"),
            "height": spec.get("height"),
            "accept_metered_generation": bool(spec.get("accept_metered_generation", False)),
            "aspect_ratio": spec.get("aspect_ratio"),
            "idempotency_key": external_idempotency_key,
        }
        if acknowledge_metered_generation:
            image_request["acknowledge_metered_generation"] = True
        return image_request

    @staticmethod
    def _call_submit_adapter(
        adapter: GenerationStudioAdapter,
        scope: StudioScope,
        request: Mapping[str, Any],
        *,
        kind: str = "image",
    ) -> Any:
        """Invoke either supported adapter spelling without duplicating I/O.

        The public contract names the first argument ``binding`` while older
        in-tree fakes used ``request, *, scope``.  Inspecting the bound method
        before invocation lets rolling deployments support both forms without
        catching a provider ``TypeError`` and accidentally submitting twice.
        """

        method = getattr(adapter, "submit_video" if kind == "video" else "submit_image", None)
        if not callable(method):
            return {"status": "unavailable", "reason_code": f"{kind}_adapter_unavailable"}
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        names = {parameter.name for parameter in parameters}
        if "scope" in names:
            return method(request, scope=scope)
        if "binding" in names or "studio_scope" in names:
            return method(scope, request)
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        if len(positional) >= 2:
            return method(scope, request)
        # Preserve the legacy request-first fallback for opaque mocks.
        return method(request, scope=scope)

    @staticmethod
    def _call_get_run_adapter(
        adapter: GenerationStudioAdapter,
        scope: StudioScope,
        run_id: str,
        *,
        kind: str = "image",
    ) -> Any:
        method = getattr(
            adapter,
            "reconcile_video" if kind == "video" else "get_run",
            None,
        )
        if not callable(method):
            return {"status": "unavailable", "reason_code": f"{kind}_adapter_unavailable"}
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        names = {parameter.name for parameter in parameters}
        if "scope" in names:
            return method(run_id, scope=scope)
        if "binding" in names or "studio_scope" in names:
            return method(scope, run_id)
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        if len(positional) >= 2:
            return method(scope, run_id)
        return method(run_id, scope=scope)

    def _normalize_adapter_response(
        self,
        response: Any,
        scope: StudioScope,
        *,
        expected_run_id: str | None = None,
        kind: str = "image",
    ) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise MediaOperationsValidationError("Generation Studio response must be an object")
        keys = {str(key) for key in response.keys()}
        if keys - _ALLOWED_ADAPTER_KEYS:
            raise MediaOperationsValidationError("Generation Studio response contains unsupported fields")
        status = str(response.get("status") or "").strip().lower()
        if status not in _STATUSES:
            raise MediaOperationsValidationError("Generation Studio response has an unsupported status")
        run_id = response.get("run_id")
        if run_id is None and status not in {"unavailable", "uncertain"}:
            raise MediaOperationsValidationError("Generation Studio response must include run_id")
        if run_id is not None:
            run_id = _opaque_id(run_id, "run_", "run_id")
            if expected_run_id is not None and run_id != expected_run_id:
                raise MediaOperationsValidationError("Generation Studio returned a different run_id")
        workspace_id = response.get("workspace_id")
        project_id = response.get("project_id")
        if run_id is not None:
            if workspace_id != scope.workspace_id:
                raise MediaOperationsValidationError("Generation Studio response workspace scope mismatch")
            if project_id != scope.project_id:
                raise MediaOperationsValidationError("Generation Studio response project scope mismatch")

        executor_kind = response.get("executor_kind")
        if executor_kind is not None:
            executor_kind = _required_text(executor_kind, "executor_kind", 32).lower()
            allowed_executors = {"comfy", "remote_image"} if kind == "image" else {"remote_video", "comfy_video"}
            if executor_kind not in allowed_executors:
                raise MediaOperationsValidationError("Generation Studio executor_kind is unsupported")
        progress = response.get("progress")
        if progress is not None:
            if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= float(progress) <= 1:
                raise MediaOperationsValidationError("Generation Studio progress is out of range")
            progress = float(progress)
        attempt = response.get("attempt")
        if attempt is not None:
            if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt <= 1000:
                raise MediaOperationsValidationError("Generation Studio attempt is out of range")
        cancel_requested = response.get("cancel_requested")
        if cancel_requested is not None and not isinstance(cancel_requested, bool):
            raise MediaOperationsValidationError("Generation Studio cancel_requested must be boolean")
        output_asset_ids = self._normalize_external_id_list(
            response.get("output_asset_ids"), "ast_", "output_asset_ids"
        )
        output_version_ids = self._normalize_external_id_list(
            response.get("output_version_ids"), "outv_", "output_version_ids"
        )
        outputs_raw = response.get("outputs", [])
        if outputs_raw is None:
            outputs_raw = []
        if isinstance(outputs_raw, (str, bytes)) or not isinstance(outputs_raw, Sequence):
            raise MediaOperationsValidationError("Generation Studio outputs must be a list")
        if len(outputs_raw) > 20:
            raise MediaOperationsValidationError("Generation Studio returned too many outputs")
        outputs: list[dict[str, Any]] = []
        for raw in outputs_raw:
            if not isinstance(raw, Mapping):
                raise MediaOperationsValidationError("Generation Studio output must be an object")
            if {str(key) for key in raw.keys()} - _ALLOWED_OUTPUT_KEYS:
                raise MediaOperationsValidationError("Generation Studio output contains unsupported fields")
            asset_id = _opaque_id(raw.get("asset_id"), "ast_", "output.asset_id")
            output_version = _opaque_id(raw.get("output_version"), "outv_", "output.output_version")
            sha256 = _validated_sha256(raw.get("sha256"), "output.sha256")
            mime_type = _required_text(raw.get("mime_type"), "output.mime_type", 255).lower()
            if kind == "image":
                if not _IMAGE_MIME_RE.fullmatch(mime_type):
                    raise MediaOperationsValidationError("image output MIME is unsupported")
            elif mime_type not in _VIDEO_MIME_TYPES:
                raise MediaOperationsValidationError("video output MIME is unsupported")
            width = raw.get("width")
            height = raw.get("height")
            for value, label in ((width, "output.width"), (height, "output.height")):
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 16384):
                    raise MediaOperationsValidationError(f"{label} is out of range")
            deep_link = None if raw.get("deep_link") is None else _validate_safe_url(raw.get("deep_link"), "output.deep_link")
            provenance_hash = raw.get("provenance_hash")
            if provenance_hash is None:
                provenance_hash = sha256_json({"asset_id": asset_id, "output_version": output_version, "sha256": sha256, "mime_type": mime_type, "width": width, "height": height, "deep_link": deep_link})
            else:
                provenance_hash = _validated_sha256(provenance_hash, "output.provenance_hash")
            outputs.append({"asset_id": asset_id, "output_version": output_version, "sha256": sha256, "mime_type": mime_type, "width": width, "height": height, "deep_link": deep_link, "provenance_hash": provenance_hash})
        cost_summary = response.get("cost_summary", {})
        if not isinstance(cost_summary, Mapping):
            raise MediaOperationsValidationError("cost_summary must be an object")
        cost_summary = _safe_json(cost_summary, "cost_summary")
        result_link = None if response.get("result_deep_link") is None else _validate_safe_url(response.get("result_deep_link"), "result_deep_link")
        return {
            "status": status,
            "run_id": run_id,
            "workspace_id": scope.workspace_id if run_id is not None else None,
            "project_id": scope.project_id if run_id is not None else None,
            "executor_kind": executor_kind,
            "progress": progress,
            "attempt": attempt,
            "cancel_requested": cancel_requested,
            "output_asset_ids": output_asset_ids,
            "output_version_ids": output_version_ids,
            "outputs": outputs,
            "adapter_release": _optional_text(response.get("adapter_release"), "adapter_release", 128),
            "started_at": _parse_datetime(response.get("started_at"), "started_at"),
            "finished_at": _parse_datetime(response.get("finished_at"), "finished_at"),
            "cost_summary": cost_summary,
            "error_code": _optional_text(response.get("error_code") or response.get("reason_code"), "error_code", 128),
            "result_deep_link": result_link,
        }

    @staticmethod
    def _normalize_external_id_list(
        value: Any, prefix: str, label: str
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise MediaOperationsValidationError(f"Generation Studio {label} must be a list")
        if len(value) > 20:
            raise MediaOperationsValidationError(f"Generation Studio {label} has too many items")
        return [_opaque_id(item, prefix, f"{label}[{index}]") for index, item in enumerate(value)]

    async def submit_generation_plan(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        plan_id: UUID | str | None = None,
        *,
        expected_plan_hash: Any | None = None,
        acknowledge_metered_generation: Any = False,
        idempotency_key: Any | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or plan_id is None:
            raise MediaOperationsValidationError("actor and plan_id are required")
        if not self._human_or_admin(actor):
            raise MediaOperationsAuthorizationError(
                "generation submit requires a human or admin principal"
            )
        if not isinstance(acknowledge_metered_generation, bool):
            raise MediaOperationsValidationError(
                "acknowledge_metered_generation must be boolean"
            )
        if idempotency_key is not None:
            _idempotency_key(idempotency_key)
        plan = await self._get_row(session, GenerationPlan, plan_id, "plan_id", for_update=True)
        actor_id = await self._assert_entity_access(session, actor, plan, permission="write")
        if expected_plan_hash is not None:
            expected_hash = _validated_sha256(expected_plan_hash, "expected_plan_hash")
            if expected_hash != plan.plan_hash:
                raise MediaOperationsConflictError(
                    "GenerationPlan changed; refresh before submitting"
                )
        workspace = await self._get_row(session, GenerationWorkspace, plan.workspace_id, "workspace_id")
        recipe_revision = await self._get_row(session, CreativeRecipeRevision, plan.creative_recipe_revision_id, "creative_recipe_revision_id")
        generation_kind = "video" if recipe_revision.recipe_type == "video" else "image"
        await self._assert_entity_access(session, actor, workspace, permission="read")
        if workspace.external_project_id is None:
            raise MediaOperationsValidationError(
                "Generation Studio project scope is required before submission"
            )
        workspace_ref = _opaque_id(
            workspace.external_workspace_id,
            "wsp_",
            "external_workspace_id",
        )
        project_ref = _opaque_id(
            workspace.external_project_id,
            "prj_",
            "external_project_id",
        )
        spec = dict(plan.request_spec or {})
        cost_policy = recipe_revision.cost_policy if isinstance(recipe_revision.cost_policy, Mapping) else {}
        metered = bool(
            spec.get("accept_metered_generation")
            or cost_policy.get("metered")
            or cost_policy.get("requires_metered_ack")
        )
        if metered and not spec.get("accept_metered_generation"):
            raise MediaOperationsValidationError(
                "metered generation requires an explicit request hint"
            )
        if metered and not acknowledge_metered_generation:
            raise MediaOperationsAuthorizationError(
                "metered generation requires explicit acknowledgement"
            )
        intent_request_hash = self._intent_request_hash(
            plan,
            recipe_revision,
            generation_kind,
            acknowledge_metered_generation=acknowledge_metered_generation,
        )
        intent = await self._scalar(session, select(GenerationRunIntent).where(GenerationRunIntent.plan_id == plan.id).limit(1))
        if intent is not None:
            if intent.request_hash != intent_request_hash:
                raise MediaOperationsConflictError(
                    "GenerationRunIntent already exists for a different semantic request"
                )
            return await self._submit_envelope(session, plan, intent)

        external_key = sha256_json({"operation": f"media.generate.{generation_kind}", "plan_id": str(plan.id), "plan_hash": plan.plan_hash})
        intent = GenerationRunIntent(
            id=uuid4(), plan_id=plan.id, owner_user_id=actor_id, project_id=plan.project_id,
            external_idempotency_key=external_key, request_hash=intent_request_hash, status="pending", created_by=actor_id,
        )
        session.add(intent)
        # Commit the durable intent before any provider call.
        await self._flush_commit(session)
        scope = StudioScope(workspace_ref, project_ref, workspace.base_url)
        request = self._adapter_request(
            plan,
            recipe_revision,
            external_key,
            acknowledge_metered_generation=acknowledge_metered_generation,
        )
        try:
            raw_response = self._call_submit_adapter(
                self.adapter,
                scope,
                request,
                kind=generation_kind,
            )
            response = await raw_response if inspect.isawaitable(raw_response) else raw_response
        except Exception:  # response may have been accepted; never retry blindly
            intent.status = "uncertain"
            intent.error_code = "adapter_response_unknown"
            intent.response_hash = sha256_json({"status": "uncertain", "error_code": "adapter_response_unknown"})
            plan.status = "submitted"
            await self._flush_commit(session)
            return await self._submit_envelope(session, plan, intent)
        try:
            normalized = self._normalize_adapter_response(
                response,
                scope,
                kind=generation_kind,
            )
        except MediaOperationsValidationError:
            # A response that is present but violates the typed contract is a
            # deterministic adapter failure, not permission to retry a submit.
            intent.status = "failed"
            intent.error_code = "adapter_contract_violation"
            intent.response_hash = sha256_json({"status": "failed", "error_code": "adapter_contract_violation"})
            plan.status = "submitted"
            await self._flush_commit(session)
            raise

        response_hash = sha256_json(normalized)
        intent.response_hash = response_hash
        intent.adapter_release = normalized["adapter_release"]
        intent.external_run_id = normalized["run_id"]
        intent.error_code = normalized["error_code"]
        intent.status = "unavailable" if normalized["status"] == "unavailable" else ("uncertain" if normalized["status"] == "uncertain" else ("failed" if normalized["status"] == "failed" else "submitted"))
        plan.status = "unavailable" if normalized["status"] == "unavailable" else "submitted"

        run: GenerationRun | None = None
        if normalized["run_id"] is not None:
            run_hash = sha256_json({"plan_id": str(plan.id), "intent_id": str(intent.id), "workspace_id": str(workspace.id), **normalized})
            run = GenerationRun(
                id=uuid4(), plan_id=plan.id, intent_id=intent.id, owner_user_id=plan.owner_user_id, project_id=plan.project_id,
                workspace_id=workspace.id, external_run_id=normalized["run_id"], external_workspace_id=scope.workspace_id,
                external_project_id=scope.project_id, status=normalized["status"], run_hash=run_hash, adapter_release=normalized["adapter_release"],
                started_at=normalized["started_at"], finished_at=normalized["finished_at"], cost_summary=normalized["cost_summary"],
                error_code=normalized["error_code"], result_deep_link=normalized["result_deep_link"], created_by=actor_id,
            )
            session.add(run)
            observation = GenerationRunObservation(
                id=uuid4(), generation_run_id=run.id, owner_user_id=plan.owner_user_id, project_id=plan.project_id,
                external_run_id=normalized["run_id"], status=normalized["status"], observation_hash=response_hash,
                adapter_release=normalized["adapter_release"], cost_summary=normalized["cost_summary"], error_code=normalized["error_code"],
                result_deep_link=normalized["result_deep_link"],
            )
            session.add(observation)
            for output in normalized["outputs"]:
                session.add(self._build_output(run, plan, output))
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            # The intent remains durable; recover an already committed receipt.
            recovered_intent = await self._scalar(session, select(GenerationRunIntent).where(GenerationRunIntent.plan_id == plan.id).limit(1))
            if recovered_intent is not None:
                recovered_run = await self._scalar(session, select(GenerationRun).where(GenerationRun.intent_id == recovered_intent.id).limit(1))
                return {"plan": await self._plan_detail(session, plan), "intent": recovered_intent.to_safe_dict(), "run": await self._run_detail(session, recovered_run) if recovered_run else None, "outputs": [], "status": recovered_intent.status, "external_idempotency_key": recovered_intent.external_idempotency_key}
            raise MediaOperationsConflictError("Generation Studio receipt conflicts with an existing record") from exc
        return await self._submit_envelope(session, plan, intent)

    async def _submit_envelope(self, session: Any, plan: GenerationPlan, intent: GenerationRunIntent) -> dict[str, Any]:
        run = await self._scalar(session, select(GenerationRun).where(GenerationRun.intent_id == intent.id).limit(1))
        detail = await self._run_detail(session, run) if run else None
        return {"plan": await self._plan_detail(session, plan), "intent": intent.to_safe_dict(), "run": detail, "outputs": detail.get("outputs", []) if detail else [], "status": intent.status, "external_idempotency_key": intent.external_idempotency_key}

    def _build_output(self, run: GenerationRun, plan: GenerationPlan, output: Mapping[str, Any]) -> GenerationOutput:
        return GenerationOutput(
            id=uuid4(), generation_run_id=run.id, owner_user_id=plan.owner_user_id, project_id=plan.project_id,
            external_asset_id=output["asset_id"], external_output_version=output["output_version"], sha256=output["sha256"],
            mime_type=output["mime_type"], width=output["width"], height=output["height"], deep_link=output["deep_link"], provenance_hash=output["provenance_hash"],
        )

    async def _run_detail(self, session: Any, run: GenerationRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        observations = await self._scalars(session, select(GenerationRunObservation).where(GenerationRunObservation.generation_run_id == run.id).order_by(GenerationRunObservation.observed_at.asc(), GenerationRunObservation.id.asc()).limit(101))
        outputs = await self._scalars(session, select(GenerationOutput).where(GenerationOutput.generation_run_id == run.id).order_by(GenerationOutput.created_at.asc(), GenerationOutput.id.asc()).limit(101))
        result = run.to_safe_dict()
        if observations:
            latest = observations[-1]
            result.update({"status": latest.status, "adapter_release": latest.adapter_release, "cost_summary": latest.cost_summary or {}, "error_code": latest.error_code, "result_deep_link": latest.result_deep_link})
        result["outputs"] = [output.to_safe_dict() for output in outputs[:100]]
        result["observations"] = [item.to_safe_dict() for item in observations[:100]]
        result["observation_history_truncated"] = len(observations) > 100
        return result

    async def list_generation_runs(self, session: Any | None = None, actor: Any | None = None, *, plan_id: UUID | str | None = None, project_id: UUID | str | None = None, limit: Any = 100, offset: Any = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        extra: list[Any] = []
        if plan_id is not None:
            plan_uuid = _as_uuid(plan_id, "plan_id")
            extra.append(GenerationRun.plan_id == plan_uuid)
        rows = await self._visible(session, actor, GenerationRun, project_id=project_uuid, limit=limit, offset=offset, extra=extra)
        return [detail for row in rows if (detail := await self._run_detail(session, row)) is not None]

    async def get_generation_run(self, session: Any | None = None, actor: Any | None = None, run_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError("actor and run_id are required")
        run = await self._get_row(session, GenerationRun, run_id, "run_id")
        await self._assert_entity_access(session, actor, run, permission="read")
        detail = await self._run_detail(session, run)
        assert detail is not None
        return detail

    async def refresh_generation_run(self, session: Any | None = None, actor: Any | None = None, run_id: UUID | str | None = None, *, idempotency_key: Any | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError("actor and run_id are required")
        if not self._human_or_admin(actor):
            raise MediaOperationsAuthorizationError(
                "generation reconcile requires a human or admin principal"
            )
        if idempotency_key is not None:
            _idempotency_key(idempotency_key)
        run = await self._get_row(session, GenerationRun, run_id, "run_id")
        await self._assert_entity_access(session, actor, run, permission="write")
        plan = await self._get_row(session, GenerationPlan, run.plan_id, "plan_id")
        recipe_revision = await self._get_row(
            session,
            CreativeRecipeRevision,
            plan.creative_recipe_revision_id,
            "creative_recipe_revision_id",
        )
        generation_kind = "video" if recipe_revision.recipe_type == "video" else "image"
        workspace = await self._get_row(session, GenerationWorkspace, run.workspace_id, "workspace_id")
        workspace_ref = _opaque_id(
            workspace.external_workspace_id,
            "wsp_",
            "external_workspace_id",
        )
        if workspace.external_project_id is None:
            raise MediaOperationsValidationError(
                "Generation Studio project scope is required before refresh"
            )
        project_ref = _opaque_id(
            workspace.external_project_id,
            "prj_",
            "external_project_id",
        )
        scope = StudioScope(workspace_ref, project_ref, workspace.base_url)
        try:
            raw_response = self._call_get_run_adapter(
                self.adapter,
                scope,
                run.external_run_id,
                kind=generation_kind,
            )
            response = await raw_response if inspect.isawaitable(raw_response) else raw_response
        except Exception:
            normalized = {"status": "uncertain", "run_id": run.external_run_id, "workspace_id": scope.workspace_id, "project_id": scope.project_id, "outputs": [], "adapter_release": None, "started_at": None, "finished_at": None, "cost_summary": {}, "error_code": "adapter_response_unknown", "result_deep_link": None}
        else:
            # Scope/opaque-ID violations are surfaced as validation errors and
            # never converted into a successful or retryable observation.
            normalized = self._normalize_adapter_response(
                response,
                scope,
                expected_run_id=run.external_run_id,
                kind=generation_kind,
            )
        observation_hash = sha256_json(normalized)
        existing = await self._scalar(session, select(GenerationRunObservation).where(GenerationRunObservation.generation_run_id == run.id, GenerationRunObservation.observation_hash == observation_hash).limit(1))
        if existing is None:
            session.add(GenerationRunObservation(id=uuid4(), generation_run_id=run.id, owner_user_id=run.owner_user_id, project_id=run.project_id, external_run_id=run.external_run_id, status=normalized["status"], observation_hash=observation_hash, adapter_release=normalized["adapter_release"], cost_summary=normalized["cost_summary"], error_code=normalized["error_code"], result_deep_link=normalized["result_deep_link"]))
            for output in normalized["outputs"]:
                prior = await self._scalar(session, select(GenerationOutput).where(GenerationOutput.generation_run_id == run.id, GenerationOutput.external_asset_id == output["asset_id"], GenerationOutput.external_output_version == output["output_version"]).limit(1))
                if prior is None:
                    session.add(self._build_output(run, run, output))
                elif prior.provenance_hash != output["provenance_hash"]:
                    raise MediaOperationsConflictError("Generation Studio attempted to mutate an existing output")
            await self._flush_commit(session)
        detail = await self._run_detail(session, run)
        assert detail is not None
        return detail

    async def reconcile_generation_run(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        run_id: UUID | str | None = None,
        *,
        expected_plan_hash: Any,
        expected_intent_request_hash: Any,
        acknowledge_metered_generation: Any = False,
        idempotency_key: Any | None = None,
    ) -> dict[str, Any]:
        """Explicitly reconcile an uncertain run without submitting again.

        Reconciliation is a read against 73 followed by an append-only local
        observation.  The original durable intent/external idempotency key is
        reused; a lost submit response can therefore never cause a second
        paid generation.
        """

        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError("actor and run_id are required")
        if not self._human_or_admin(actor):
            raise MediaOperationsAuthorizationError(
                "generation reconcile requires a human or admin principal"
            )
        if not isinstance(acknowledge_metered_generation, bool):
            raise MediaOperationsValidationError(
                "acknowledge_metered_generation must be boolean"
            )
        if idempotency_key is not None:
            _idempotency_key(idempotency_key)
        expected_plan = _validated_sha256(expected_plan_hash, "expected_plan_hash")
        expected_intent = _validated_sha256(
            expected_intent_request_hash,
            "expected_intent_request_hash",
        )
        run = await self._get_row(
            session, GenerationRun, run_id, "run_id", for_update=True
        )
        await self._assert_entity_access(session, actor, run, permission="write")
        plan = await self._get_row(session, GenerationPlan, run.plan_id, "plan_id")
        if plan.plan_hash != expected_plan:
            raise MediaOperationsConflictError(
                "GenerationPlan changed; refresh before reconciling"
            )
        intent = await self._scalar(
            session,
            select(GenerationRunIntent)
            .where(GenerationRunIntent.id == run.intent_id)
            .limit(1),
        )
        if intent is None:
            raise MediaOperationsConflictError("generation intent is missing")
        if intent.request_hash != expected_intent:
            raise MediaOperationsConflictError(
                "GenerationRunIntent changed; refresh before reconciling"
            )
        recipe_revision = await self._get_row(
            session,
            CreativeRecipeRevision,
            plan.creative_recipe_revision_id,
            "creative_recipe_revision_id",
        )
        spec = dict(plan.request_spec or {})
        cost_policy = (
            recipe_revision.cost_policy
            if isinstance(recipe_revision.cost_policy, Mapping)
            else {}
        )
        metered = bool(
            spec.get("accept_metered_generation")
            or cost_policy.get("metered")
            or cost_policy.get("requires_metered_ack")
        )
        if metered and not spec.get("accept_metered_generation"):
            raise MediaOperationsValidationError(
                "metered generation requires an explicit request hint"
            )
        if metered and not acknowledge_metered_generation:
            raise MediaOperationsAuthorizationError(
                "metered generation requires explicit acknowledgement"
            )
        generation_kind = "video" if recipe_revision.recipe_type == "video" else "image"
        workspace = await self._get_row(
            session, GenerationWorkspace, run.workspace_id, "workspace_id"
        )
        await self._assert_entity_access(session, actor, workspace, permission="read")
        if workspace.external_project_id is None:
            raise MediaOperationsValidationError(
                "Generation Studio project scope is required before reconcile"
            )
        scope = StudioScope(
            _opaque_id(workspace.external_workspace_id, "wsp_", "external_workspace_id"),
            _opaque_id(workspace.external_project_id, "prj_", "external_project_id"),
            workspace.base_url,
        )
        try:
            raw_response = self._call_get_run_adapter(
                self.adapter,
                scope,
                run.external_run_id,
                kind=generation_kind,
            )
            response = await raw_response if inspect.isawaitable(raw_response) else raw_response
        except Exception:
            # A reconcile transport timeout is still uncertain.  It is an
            # explicit user command, but must not turn into an implicit retry.
            intent.status = "uncertain"
            intent.error_code = "adapter_response_unknown"
            intent.response_hash = sha256_json(
                {"status": "uncertain", "error_code": "adapter_response_unknown"}
            )
            await self._flush_commit(session)
            return await self._submit_envelope(session, plan, intent)
        normalized = self._normalize_adapter_response(
            response,
            scope,
            expected_run_id=run.external_run_id,
            kind=generation_kind,
        )
        observation_hash = sha256_json(normalized)
        existing = await self._scalar(
            session,
            select(GenerationRunObservation)
            .where(
                GenerationRunObservation.generation_run_id == run.id,
                GenerationRunObservation.observation_hash == observation_hash,
            )
            .limit(1),
        )
        if existing is None:
            session.add(
                GenerationRunObservation(
                    id=uuid4(),
                    generation_run_id=run.id,
                    owner_user_id=run.owner_user_id,
                    project_id=run.project_id,
                    external_run_id=run.external_run_id,
                    status=normalized["status"],
                    observation_hash=observation_hash,
                    adapter_release=normalized["adapter_release"],
                    cost_summary=normalized["cost_summary"],
                    error_code=normalized["error_code"],
                    result_deep_link=normalized["result_deep_link"],
                )
            )
            for output in normalized["outputs"]:
                prior = await self._scalar(
                    session,
                    select(GenerationOutput)
                    .where(
                        GenerationOutput.generation_run_id == run.id,
                        GenerationOutput.external_asset_id == output["asset_id"],
                        GenerationOutput.external_output_version == output["output_version"],
                    )
                    .limit(1),
                )
                if prior is None:
                    session.add(self._build_output(run, plan, output))
                elif prior.provenance_hash != output["provenance_hash"]:
                    raise MediaOperationsConflictError(
                        "Generation Studio attempted to mutate an existing output"
                    )
        intent.response_hash = observation_hash
        intent.adapter_release = normalized["adapter_release"]
        intent.error_code = normalized["error_code"]
        intent.status = (
            "unavailable"
            if normalized["status"] == "unavailable"
            else "uncertain"
            if normalized["status"] == "uncertain"
            else "failed"
            if normalized["status"] == "failed"
            else "submitted"
        )
        plan.status = "unavailable" if normalized["status"] == "unavailable" else "submitted"
        await self._flush_commit(session)
        return await self._submit_envelope(session, plan, intent)

    async def reconcile_generation_plan(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        plan_id: UUID | str | None = None,
        *,
        expected_plan_hash: Any,
        expected_intent_request_hash: Any,
        acknowledge_metered_generation: Any = False,
        idempotency_key: Any | None = None,
    ) -> dict[str, Any]:
        """Reconcile a plan-addressed durable receipt.

        The HTTP contract addresses reconciliation by ``plan_id`` while the
        ledger observation is keyed by its opaque ``run_id``.  Resolve that
        local relationship without contacting 73, then delegate to the
        run-addressed implementation so there is exactly one reconciliation
        path and no accidental submit retry.
        """

        session = self._resolve_session(session)
        if actor is None or plan_id is None:
            raise MediaOperationsValidationError("actor and plan_id are required")
        if not self._human_or_admin(actor):
            raise MediaOperationsAuthorizationError(
                "generation reconcile requires a human or admin principal"
            )
        plan = await self._get_row(session, GenerationPlan, plan_id, "plan_id")
        await self._assert_entity_access(session, actor, plan, permission="write")
        run = await self._scalar(
            session,
            select(GenerationRun)
            .where(GenerationRun.plan_id == plan.id)
            .order_by(GenerationRun.created_at.asc(), GenerationRun.id.asc())
            .limit(1),
        )
        if run is None:
            raise MediaOperationsConflictError(
                "GenerationPlan has no external run to reconcile"
            )
        return await self.reconcile_generation_run(
            session,
            actor,
            run.id,
            expected_plan_hash=expected_plan_hash,
            expected_intent_request_hash=expected_intent_request_hash,
            acknowledge_metered_generation=acknowledge_metered_generation,
            idempotency_key=idempotency_key,
        )

    async def select_generation_output(self, session: Any | None = None, actor: Any | None = None, run_id: UUID | str | None = None, output_id: UUID | str | None = None, *, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None or output_id is None:
            raise MediaOperationsValidationError("actor, run_id, and output_id are required")
        if not self._human_or_admin(actor):
            raise MediaOperationsAuthorizationError(
                "generation output selection requires a human or admin principal"
            )
        run = await self._get_row(session, GenerationRun, run_id, "run_id")
        actor_id = await self._assert_entity_access(session, actor, run, permission="write")
        output = await self._get_row(session, GenerationOutput, output_id, "output_id")
        await self._assert_entity_access(session, actor, output, permission="write")
        if output.generation_run_id != run.id:
            raise MediaOperationsValidationError("output does not belong to run")
        plan = await self._get_row(session, GenerationPlan, run.plan_id, "plan_id")
        recipe_revision = await self._get_row(
            session,
            CreativeRecipeRevision,
            plan.creative_recipe_revision_id,
            "creative_recipe_revision_id",
        )
        generation_kind = "video" if recipe_revision.recipe_type == "video" else "image"
        mime_type = str(output.mime_type or "").strip().lower()
        if generation_kind == "image":
            valid_mime = _IMAGE_MIME_RE.fullmatch(mime_type) is not None
        else:
            valid_mime = mime_type in _VIDEO_MIME_TYPES
        if not valid_mime:
            raise MediaOperationsValidationError(
                "output MIME does not match the generation kind"
            )
        persona_revision = await self._get_row(
            session,
            PersonaRevision,
            plan.persona_revision_id,
            "persona_revision_id",
        )
        await self._assert_content_graph(
            session,
            actor,
            persona_revision,
            plan.content_item_id,
            plan.content_variant_id,
        )
        key = _idempotency_key(idempotency_key)
        selection_hash = sha256_json({"run_id": str(run.id), "output_id": str(output.id), "output_provenance_hash": output.provenance_hash})
        existing = await self._scalar(session, select(GenerationOutputSelection).where(GenerationOutputSelection.generation_run_id == run.id, GenerationOutputSelection.idempotency_key == key).limit(1))
        if existing is not None:
            if existing.selection_hash != selection_hash:
                raise MediaOperationsConflictError("idempotency key was already used for a different output selection")
            return {**existing.to_safe_dict(), "output": output.to_safe_dict()}
        selection = GenerationOutputSelection(id=uuid4(), generation_run_id=run.id, generation_output_id=output.id, owner_user_id=actor_id, project_id=run.project_id, selection_hash=selection_hash, idempotency_key=key, created_by=actor_id)
        session.add(selection)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(session, select(GenerationOutputSelection).where(GenerationOutputSelection.generation_run_id == run.id, GenerationOutputSelection.idempotency_key == key).limit(1))
            if recovered is not None and recovered.selection_hash == selection_hash:
                return {**recovered.to_safe_dict(), "output": output.to_safe_dict()}
            raise MediaOperationsConflictError("output selection conflicts with an existing record") from exc
        return {**selection.to_safe_dict(), "output": output.to_safe_dict()}
