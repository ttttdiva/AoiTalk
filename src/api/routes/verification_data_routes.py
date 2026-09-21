"""Administrator-only maintenance routes for disposable verification data.

The destructive surface in this module is deliberately a *selector* surface,
not an entity-id delete endpoint.  A selector identifies either a provenance
run or a bounded, operator-supplied legacy manifest.  The verification cleanup
service re-reads the selected provenance immediately before deleting anything
and is responsible for applying the canonical Task/Project/User deletion
semantics.

Keeping the route thin is important here: accepting a project/task/user UUID
directly would turn an operator convenience endpoint into an arbitrary delete
primitive.  The preview digest and typed confirmation also make a stale or
cross-run destructive request fail closed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..router_helpers import (
    await_task_completion_before_cancellation,
    cookie_auth_dependency,
)

if TYPE_CHECKING:
    from ..server import WebChatServer


logger = logging.getLogger(__name__)

CONFIRMATION_TEXT = "DELETE VERIFIED TEST DATA"
MAX_SELECTORS = 500
MAX_SELECTOR_ID_LENGTH = 256

# Legacy manifests are names/keys resolved by the cleanup service.  Paths are
# intentionally rejected at the HTTP boundary; allowing an arbitrary path
# here would make the admin endpoint a file-read/delete primitive.
_LEGACY_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LEGACY_MANIFEST_ROOT = (
    Path(__file__).resolve().parents[3] / "scripts" / "verification" / "manifests"
)


class VerificationDataSelector(BaseModel):
    """A provenance selector accepted by the destructive endpoint."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["legacy_manifest", "verification_run"]
    id: str = Field(min_length=1, max_length=MAX_SELECTOR_ID_LENGTH)

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("selector id must not be empty")
        return value

    @model_validator(mode="after")
    def validate_selector_identity(self) -> "VerificationDataSelector":
        if self.type == "verification_run":
            try:
                # Canonicalize equivalent UUID spellings so the aggregate
                # digest and allow-list comparison cannot reject a harmless
                # upper-case selector sent by an API client.
                self.id = str(UUID(self.id))
            except (TypeError, ValueError) as exc:
                raise ValueError("verification_run selector id must be a UUID") from exc
        elif not _LEGACY_SELECTOR_RE.fullmatch(self.id):
            raise ValueError("legacy_manifest selector id is not a valid manifest key")
        return self


class VerificationDataCleanupPayload(BaseModel):
    """Typed confirmation required for destructive cleanup."""

    model_config = ConfigDict(extra="forbid")

    selectors: list[VerificationDataSelector] = Field(
        min_length=1,
        max_length=MAX_SELECTORS,
    )
    preview_digest: str = Field(min_length=1, max_length=512)
    confirmation: Literal[CONFIRMATION_TEXT]

    @field_validator("preview_digest")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("preview_digest must not be empty")
        return value

    @model_validator(mode="after")
    def reject_duplicate_selectors(self) -> "VerificationDataCleanupPayload":
        seen: set[tuple[str, str]] = set()
        for selector in self.selectors:
            key = (selector.type, selector.id)
            if key in seen:
                raise ValueError("duplicate verification selector")
            seen.add(key)
        return self


def _canonical_digest(value: Any) -> str:
    """Produce a stable, secret-free digest for a preview projection."""

    if isinstance(value, Mapping):
        normalized = {
            str(key): value[key]
            for key in sorted(value, key=lambda item: str(item))
            if str(key).lower() not in {"secret", "password", "token"}
        }
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = list(value)
    else:
        normalized = value
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _selector_key(selector: VerificationDataSelector) -> str:
    return f"{selector.type}:{selector.id}"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _manifest_from_provenance_preview(preview: Mapping[str, Any], *, run_id: UUID) -> Mapping[str, Any]:
    """Convert a durable provenance projection into a cleanup manifest.

    The provenance ledger intentionally stores one row per artifact rather
    than a second mutable manifest document.  The cleanup service consumes its
    immutable manifest shape, so this bridge copies only the allow-listed
    identity/metadata fields and assigns category A from the ledger's explicit
    ``disposable=true`` run invariant.  It never accepts entity IDs from the
    HTTP request itself.
    """

    run = preview.get("run")
    if not isinstance(run, Mapping):
        raise HTTPException(status_code=404, detail="Verification run not found")
    raw_run_id = run.get("run_id") or run.get("id")
    if str(raw_run_id) != str(run_id):
        raise HTTPException(status_code=409, detail="Verification run identity changed")
    if run.get("disposable") is not True:
        raise HTTPException(status_code=403, detail="Verification run is not disposable")
    source = run.get("source") or run.get("harness")
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(status_code=409, detail="Verification run source is missing")
    artifacts = preview.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes, bytearray)):
        raise HTTPException(status_code=409, detail="Verification run artifacts are unavailable")
    entities: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        entity_type = str(artifact.get("entity_type") or "").strip().casefold().replace("-", "_")
        if entity_type not in {"project", "task", "user"}:
            # Provenance may intentionally cover files, docs, or runs which
            # have their own cleanup owner.  The coordinator's project/task/
            # user graph is not allowed to delete those rows accidentally.
            continue
        entity_id = artifact.get("entity_id")
        if entity_id is None:
            continue
        entity_id = str(entity_id).strip()
        if not entity_id:
            continue
        metadata = artifact.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        entities.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "category": "A",
                "metadata": dict(metadata),
            }
        )
    if not entities:
        raise HTTPException(status_code=409, detail="Verification run has no deletable artifacts")
    return {
        "schema_version": 1,
        "manifest_id": str(run_id),
        "run_id": str(run_id),
        "source": source.strip(),
        "disposable": True,
        "created_at": run.get("created_at"),
        "entities": entities,
        "metadata": run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {},
    }


def _invoke_service(method: Any, session: Any, kwargs: Mapping[str, Any]) -> Any:
    """Call a service method once, adapting only its declared parameters.

    Avoid catching a ``TypeError`` raised *inside* a deletion transaction and
    retrying it with a different signature: a partially executed cleanup must
    never be invoked twice.  Signatures of bound methods are available for all
    in-tree services and test doubles; opaque callables receive the canonical
    positional form.
    """

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(session, **dict(kwargs))
    parameters = signature.parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    filtered = dict(kwargs) if accepts_kwargs else {
        name: value for name, value in kwargs.items() if name in parameters
    }
    session_parameter = parameters.get("session")
    if session_parameter is not None and session_parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    }:
        return method(session, **filtered)
    if session_parameter is not None:
        filtered["session"] = session
        return method(**filtered)
    # Compatibility with older helpers that call the first argument `db` or
    # `_session`; positional invocation is the least surprising fallback.
    return method(session, **filtered)


def _service_error_to_http(exc: BaseException) -> HTTPException:
    """Map typed cleanup failures without turning malformed input into 500."""

    if isinstance(exc, HTTPException):
        return exc
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "http_status", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    error_code = getattr(exc, "code", None)
    if exc.__class__.__name__ in {"VerificationRunNotFound", "VerificationManifestNotFound"}:
        status_code = 404
    elif exc.__class__.__name__ in {
        "VerificationProvenanceConflict",
        "VerificationCleanupConflict",
    }:
        status_code = 409
    elif (
        exc.__class__.__name__ in {"VerificationProvenanceError", "VerificationCleanupError"}
        and "not disposable" in str(exc).casefold()
    ):
        status_code = 403
    elif error_code in {"manifest_unreadable", "manifest_not_found"}:
        # A bounded, allow-listed legacy key that has no checked-in manifest
        # is a not-found resource, not malformed caller input.  Distinguish
        # it from ``manifest_invalid_*`` (an existing but corrupt file),
        # which remains a 400 below.
        status_code = 404
    elif status_code in {413, 422}:
        # FastAPI's public contract uses 400 for malformed operator manifests;
        # keep internal validation granularity out of the HTTP surface.
        status_code = 400
    if status_code not in {400, 403, 404, 409, 503}:
        if isinstance(exc, (ValueError, TypeError)):
            status_code = 400
        elif isinstance(exc, (FileNotFoundError, LookupError)):
            status_code = 404
        elif isinstance(exc, PermissionError):
            status_code = 403
        else:
            status_code = 500
    # Cleanup service errors are authored for operator consumption.  For an
    # unexpected exception use a generic message so traceback/connection
    # details can never cross the API boundary.
    detail = str(exc) if status_code != 500 and str(exc).strip() else "Verification data cleanup failed"
    return HTTPException(status_code=status_code, detail=detail)


def _load_cleanup_types() -> tuple[Any, Any, Any]:
    """Resolve optional cleanup services lazily during route invocation.

    The API server must remain bootable while a rolling deploy is applying the
    verification ledger migration.  Lazy import preserves that property and
    still gives the endpoint a clear 503 until the service is available.
    """

    try:
        from ...services.verification_cleanup import (
            VerificationCleanupCoordinator,
            VerificationCleanupError,
            load_cleanup_manifest,
        )
    except ImportError as exc:  # pragma: no cover - exercised during rollout
        raise HTTPException(
            status_code=503,
            detail="Verification data maintenance is not available",
        ) from exc
    return VerificationCleanupCoordinator, VerificationCleanupError, load_cleanup_manifest


def _load_cleanup_list_method() -> Any | None:
    """Load an optional module-level eligible-run listing helper."""

    try:
        from ...services.verification_cleanup import list_eligible
    except ImportError:
        return None
    return list_eligible


def _provenance_service_for_session(session: Any) -> Any | None:
    """Construct the ledger reader only for a real async DB-session adapter.

    Lightweight route tests and rolling-deploy shims often expose a ``scalar``
    attribute for unrelated reasons but do not implement the full SQLAlchemy
    ``execute``/``scalars`` surface.  Calling the provenance ORM reader on
    those adapters would turn an otherwise valid coordinator preview into a
    spurious 404/500.  The production ``AsyncSession`` exposes all three
    methods, so this narrow capability check keeps the bridge fail-closed
    without making test doubles pretend to be a database.
    """

    if not all(
        callable(getattr(session, name, None))
        for name in ("scalar", "scalars", "execute")
    ):
        return None
    try:
        from ...services.verification_provenance import VerificationProvenanceService

        return VerificationProvenanceService()
    except ImportError:
        return None


def _coordinator_for(server: "WebChatServer") -> Any:
    coordinator_type, _error_type, _loader = _load_cleanup_types()
    # The coordinator owns canonical deletion services.  The constructor is
    # intentionally dependency-light; passing these repositories by keyword
    # also keeps test doubles and future service versions compatible.
    kwargs: dict[str, Any] = {}
    task_service = getattr(server, "_task_management_service", None)
    project_repository = getattr(server, "_project_repository", None)
    if task_service is not None:
        kwargs["task_service"] = task_service
    if project_repository is not None:
        kwargs["project_repository"] = project_repository
    try:
        return coordinator_type(**kwargs)
    except TypeError:
        return coordinator_type()


async def _actor_id(server: "WebChatServer", request: Request) -> str | None:
    state_actor = getattr(getattr(request, "state", None), "user_id", None)
    if state_actor:
        return str(state_actor)
    resolver = getattr(server, "_get_user_info_from_request", None)
    if not callable(resolver):
        return None
    try:
        try:
            value = resolver(request, allow_password_reset=True)
        except TypeError:
            value = resolver(request)
        value = await _maybe_await(value)
    except Exception:
        return None
    if isinstance(value, Mapping):
        raw = value.get("id") or value.get("user_id")
        return str(raw) if raw else None
    raw = getattr(value, "id", None) or getattr(value, "user_id", None)
    return str(raw) if raw else None


async def _open_session(server: "WebChatServer") -> Any:
    manager = getattr(server, "_db_manager", None)
    if manager is None or not callable(getattr(manager, "get_session", None)):
        raise HTTPException(
            status_code=503,
            detail="Verification data maintenance is not available (database not configured)",
        )
    try:
        return await _maybe_await(manager.get_session())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to obtain verification cleanup DB session", exc_info=True)
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc


async def _close_session(
    session: Any,
    *,
    rollback: bool = False,
    close: bool = True,
) -> None:
    if session is None:
        return
    if rollback and callable(getattr(session, "rollback", None)):
        try:
            await _maybe_await(session.rollback())
        except Exception:
            logger.warning("Verification cleanup transaction rollback failed", exc_info=True)
    if not close:
        return
    if callable(getattr(session, "close", None)):
        try:
            await _maybe_await(session.close())
        except Exception:
            logger.warning("Verification cleanup transaction close failed", exc_info=True)


async def _resolve_manifest(
    session: Any,
    selector: VerificationDataSelector,
    *,
    provenance: Any | None = None,
    load_manifest: Any | None = None,
) -> Any:
    """Resolve a selector to a bounded manifest without accepting raw IDs."""

    if selector.type == "legacy_manifest":
        if not callable(load_manifest):
            raise HTTPException(status_code=503, detail="Legacy manifest support is unavailable")
        try:
            manifest = load_manifest(selector.id)
            manifest = await _maybe_await(manifest)
            manifest_id = getattr(manifest, "manifest_id", None)
            if isinstance(manifest, Mapping):
                manifest_id = manifest_id or manifest.get("manifest_id")
            if manifest_id is not None and str(manifest_id) != selector.id:
                raise HTTPException(status_code=409, detail="Legacy manifest identity changed")
            return manifest
        except Exception as exc:
            raise _service_error_to_http(exc) from exc

    run_id = UUID(selector.id)
    # VerificationProvenanceService.preview_run is the canonical bridge from
    # a run UUID to its artifact manifest.  The cleanup coordinator performs a
    # second read/validation; this first read only resolves the selector.
    if provenance is not None and callable(getattr(provenance, "preview_run", None)):
        try:
            preview = await _maybe_await(
                _invoke_service(
                    provenance.preview_run,
                    session,
                    {"run_id": run_id},
                )
            )
        except Exception as exc:
            raise _service_error_to_http(exc) from exc
        if preview is None:
            raise HTTPException(status_code=404, detail="Verification run not found")
        if not isinstance(preview, Mapping) and callable(getattr(preview, "to_dict", None)):
            preview = await _maybe_await(preview.to_dict())
        if isinstance(preview, Mapping):
            manifest = preview.get("manifest") or preview.get("cleanup_manifest")
            if manifest is not None:
                return manifest
            # Convert the ledger's bounded artifact projection to the
            # coordinator's frozen manifest shape.  Do not synthesize entity
            # IDs from client input.
            return _manifest_from_provenance_preview(preview, run_id=run_id)
        return preview
    # Coordinator implementations may resolve run IDs internally.  The
    # mapping remains explicit and contains no entity selector fields.
    return {"run_id": str(run_id), "source": "verification_run"}


async def _preview_selector(
    coordinator: Any,
    session: Any,
    selector: VerificationDataSelector,
    *,
    provenance: Any | None,
    load_manifest: Any | None,
    actor_user_id: str | None,
    manifest: Any | None = None,
) -> dict[str, Any]:
    if manifest is None:
        manifest = await _resolve_manifest(
            session,
            selector,
            provenance=provenance,
            load_manifest=load_manifest,
        )
    try:
        preview_method = getattr(coordinator, "preview", None)
        if not callable(preview_method):
            raise HTTPException(status_code=503, detail="Verification preview is unavailable")
        result = await _maybe_await(
            _invoke_service(
                preview_method,
                session,
                {
                    "run_id": selector.id,
                    "manifest": manifest,
                    "actor_user_id": actor_user_id,
                },
            )
        )
    except Exception as exc:
        raise _service_error_to_http(exc) from exc
    if not isinstance(result, Mapping):
        raise HTTPException(status_code=500, detail="Invalid verification preview response")
    return dict(result)


async def _list_previews(
    coordinator: Any,
    session: Any,
    *,
    actor_user_id: str | None,
) -> dict[str, Any]:
    """Return all explicitly attributable runs for the Settings panel."""

    list_method = getattr(coordinator, "list_eligible", None)
    if not callable(list_method):
        list_method = _load_cleanup_list_method()
    if not callable(list_method):
        # A provenance service may expose listing while the coordinator only
        # exposes preview/execute.  Keep this fallback explicit and read-only.
        provenance_type = None
        try:
            from ...services.verification_provenance import VerificationProvenanceService

            provenance_type = VerificationProvenanceService
        except ImportError:
            provenance_type = None
        service = provenance_type() if provenance_type is not None else None
        list_method = getattr(service, "list_runs", None) if service else None
    if not callable(list_method):
        return {
            "status": "preview",
            "selectors": [],
            "runs": [],
            "counts": {},
            "preview_digest": _canonical_digest([]),
        }
    try:
        value = _invoke_service(
            list_method,
            session,
            {"actor_user_id": actor_user_id},
        )
        value = await _maybe_await(value)
    except Exception as exc:
        raise _service_error_to_http(exc) from exc
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        result = {"runs": list(value or [])}
    selectors = result.get("selectors")
    runs = result.get("runs")
    if not isinstance(runs, list):
        runs = []
        result["runs"] = runs
    # The coordinator's compatibility adapter may not yet know the
    # provenance service's canonical ``list_runs`` spelling.  Fall back to a
    # direct read-only ledger listing when its first result is empty.
    used_run_listing_fallback = False
    if not runs and (not isinstance(selectors, list) or not selectors):
        service = getattr(coordinator, "provenance", None)
        if service is None:
            try:
                from ...services.verification_provenance import VerificationProvenanceService

                service = VerificationProvenanceService()
            except ImportError:
                service = None
        list_runs = getattr(service, "list_runs", None) if service is not None else None
        if callable(list_runs):
            try:
                listed = await _maybe_await(
                    _invoke_service(
                        list_runs,
                        session,
                        {"limit": 500, "include_cleaned": True},
                    )
                )
                if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes, bytearray)):
                    runs = [
                        item.to_dict() if callable(getattr(item, "to_dict", None)) else dict(item)
                        for item in listed
                        if isinstance(item, Mapping) or callable(getattr(item, "to_dict", None))
                    ]
                    result["runs"] = runs
                    used_run_listing_fallback = True
            except Exception as exc:
                logger.info("Verification provenance run listing fallback failed: %s", exc)
    selectors = result.get("selectors")
    if not isinstance(selectors, list) or not selectors:
        selectors = []
        for run in result.get("runs", []):
            if not isinstance(run, Mapping):
                continue
            raw_id = run.get("run_id") or run.get("id")
            try:
                normalized_id = str(UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
            marker = run.get("classification", run.get("category"))
            # A ledger row is a positive provenance assertion only when its
            # durable disposable flag is literally true.  Even if a stale or
            # malformed projection claims ``classification='A'``, a
            # non-disposable row remains protected rather than becoming a
            # deletion candidate.
            if run.get("disposable") is not True:
                marker = "C"
            elif marker is None:
                marker = "A"
            elif not isinstance(marker, str):
                # An explicitly malformed marker is not equivalent to an
                # omitted marker; keep it protected until an operator fixes
                # the provenance projection.
                marker = "C"
            selectors.append(
                {
                    "type": "verification_run",
                    "id": normalized_id,
                    "classification": marker.strip().upper(),
                    "category": marker.strip().upper(),
                    "source": run.get("source") or run.get("harness"),
                    "status": run.get("status"),
                }
            )
        result["selectors"] = selectors
    elif used_run_listing_fallback:
        # The coordinator's empty compatibility response carries the digest of
        # an empty selector set; replace it after the provenance fallback.
        result["preview_digest"] = _canonical_digest(selectors)
    # Reconcile selector projections with the durable run rows whenever both
    # are present.  A stale service projection must not retain an A marker for
    # a run that has since become non-disposable; downgrade that selector to
    # protected C rather than trusting the client-visible marker blindly.
    if isinstance(selectors, list) and runs:
        run_disposable: dict[str, bool] = {}
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            raw_id = run.get("run_id") or run.get("id")
            if raw_id is not None:
                run_disposable[str(raw_id).strip().lower()] = run.get("disposable") is True
        if run_disposable:
            reconciled: list[Any] = []
            reconciled_changed = False
            for value in selectors:
                key = _selector_from_value(value)
                if (
                    key is not None
                    and key[0] == "verification_run"
                    and run_disposable.get(key[1].lower()) is False
                    and isinstance(value, Mapping)
                ):
                    protected = dict(value)
                    protected["classification"] = "C"
                    protected["category"] = "C"
                    reconciled.append(protected)
                    reconciled_changed = True
                else:
                    reconciled.append(value)
            selectors = reconciled
            result["selectors"] = selectors
            if reconciled_changed:
                result["preview_digest"] = _canonical_digest(selectors)
    result.setdefault("status", "preview")
    result.setdefault("preview_digest", _canonical_digest(result.get("selectors", [])))
    return result


def _selector_from_value(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    kind = value.get("type")
    raw_id = value.get("id") or value.get("run_id")
    if kind not in {"legacy_manifest", "verification_run"} or not raw_id:
        return None
    try:
        selector = VerificationDataSelector(type=kind, id=str(raw_id))
    except Exception:
        return None
    return selector.type, selector.id


def _is_explicit_category_a(value: Mapping[str, Any]) -> bool:
    """Return true only for an explicit A classification from the service."""

    for key in ("category", "classification", "disposition"):
        marker = value.get(key)
        if isinstance(marker, str) and marker.strip().upper() == "A":
            return True
    return False


def _eligible_a_selector_keys(preview: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Extract only A selectors from the full server-side preview.

    The Settings UI intentionally sends only the A subset while retaining the
    digest for the complete preview.  This server-side extraction prevents a
    client from upgrading a B/C selector by merely omitting its classification.
    """

    eligible: set[tuple[str, str]] = set()
    selectors = preview.get("selectors")
    if isinstance(selectors, Sequence) and not isinstance(selectors, (str, bytes, bytearray)):
        for value in selectors:
            key = _selector_from_value(value)
            if key is not None and isinstance(value, Mapping) and _is_explicit_category_a(value):
                eligible.add(key)
    for field_name in ("category_a", "a_selectors"):
        category_a = preview.get(field_name)
        if isinstance(category_a, Sequence) and not isinstance(category_a, (str, bytes, bytearray)):
            for value in category_a:
                key = _selector_from_value(value)
                if key is not None:
                    eligible.add(key)
    return eligible


def _preview_listing_digest(preview: Mapping[str, Any]) -> str:
    value = preview.get("preview_digest") or preview.get("digest")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _canonical_digest(preview.get("selectors", []))


def _legacy_manifest_keys() -> list[str]:
    """Return the checked-in one-time manifest keys, never arbitrary paths."""

    try:
        paths = sorted(_LEGACY_MANIFEST_ROOT.glob("*.json"))
    except OSError:
        return []
    result: list[str] = []
    for path in paths:
        key = path.stem
        if _LEGACY_SELECTOR_RE.fullmatch(key):
            result.append(key)
    return result


async def _append_legacy_manifest_previews(
    coordinator: Any,
    session: Any,
    result: dict[str, Any],
    *,
    actor_user_id: str | None,
    load_manifest: Any,
) -> dict[str, Any]:
    """Expose the bounded legacy manifest(s) alongside ledger-backed runs.

    Legacy cleanup is intentionally an explicit checked-in manifest, not a
    permanent name/age heuristic.  A manifest whose graph no longer matches
    the database is omitted from the deletable A selector set (the coordinator
    remains the authority and can still report the failure in its audit log).
    """

    selectors = list(result.get("selectors") or [])
    runs = list(result.get("runs") or []) if isinstance(result.get("runs"), list) else []
    added_legacy = False
    for key in _legacy_manifest_keys():
        selector_key = ("legacy_manifest", key)
        if any(_selector_from_value(value) == selector_key for value in selectors):
            continue
        try:
            manifest = load_manifest(key)
            manifest_run_id = getattr(manifest, "run_id", None)
            if isinstance(manifest, Mapping):
                manifest_run_id = manifest_run_id or manifest.get("run_id")
            manifest_run_id = str(manifest_run_id or key)
            preview = await _maybe_await(
                _invoke_service(
                    getattr(coordinator, "preview"),
                    session,
                    {
                        "run_id": manifest_run_id,
                        "manifest": manifest,
                        "actor_user_id": actor_user_id,
                    },
                )
            )
            if not isinstance(preview, Mapping):
                continue
            # A coordinator may know the manifest but mark it protected or
            # already invalid.  Never turn that negative preview into an A
            # selector merely because the static manifest key is allow-listed.
            if preview.get("eligible") is False:
                continue
            selector = {
                "type": "legacy_manifest",
                "id": key,
                "classification": "A",
                "category": "A",
                "source": preview.get("source"),
                "counts": preview.get("counts", {}),
                "preview_digest": preview.get("digest") or preview.get("preview_digest"),
            }
            selectors.append(selector)
            added_legacy = True
            runs.append(
                {
                    "run_id": manifest_run_id,
                    "manifest_id": key,
                    "source": preview.get("source"),
                    "classification": "A",
                    "category": "A",
                    "counts": preview.get("counts", {}),
                    "status": "preview",
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            # Missing/mutated legacy targets are not silently upgraded to
            # deletable data.  Keep the endpoint usable for the remaining
            # ledger runs and let the operator inspect the manifest offline.
            mapped = _service_error_to_http(exc)
            if mapped.status_code == 503:
                raise mapped from exc
            logger.info("Skipping legacy verification manifest %s: %s", key, mapped.detail)
    result["selectors"] = selectors
    result["runs"] = runs
    # The digest is over the complete server-side selector projection.  Keep a
    # coordinator/provenance digest byte-for-byte when no legacy selector was
    # added; recompute only when we extend that projection with the static
    # manifest set.
    if added_legacy:
        result["preview_digest"] = _canonical_digest(selectors)
    return result


async def _execute_selector(
    coordinator: Any,
    session: Any,
    *,
    selector: VerificationDataSelector,
    manifest: Any,
    actor_user_id: str | None,
    confirmation_digest: str,
) -> Any:
    """Invoke the coordinator's canonical execute/cleanup spelling."""

    execute = getattr(coordinator, "execute", None)
    if not callable(execute):
        execute = getattr(coordinator, "cleanup", None)
    if not callable(execute):
        raise HTTPException(status_code=503, detail="Verification cleanup is unavailable")
    kwargs = {
        "run_id": selector.id,
        "manifest": manifest,
        "actor_user_id": actor_user_id,
        "confirmation_digest": confirmation_digest,
        "dry_run": False,
    }
    return await _maybe_await(_invoke_service(execute, session, kwargs))


def register_verification_data_routes(app: FastAPI, server: "WebChatServer") -> None:
    """Register admin-only verification/test-data maintenance endpoints."""

    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def require_admin(
        request: Request,
        _auth: None = Depends(require_auth),
    ) -> None:
        if not await _maybe_await(server._is_admin_user(request)):
            raise HTTPException(status_code=403, detail="Administrator privileges required")

    @app.get("/api/admin/verification-data/preview")
    async def preview_verification_data(
        request: Request,
        run_id: str | None = Query(None, min_length=1, max_length=MAX_SELECTOR_ID_LENGTH),
        selector_type: Literal["legacy_manifest", "verification_run"] = Query(
            "verification_run"
        ),
        _: None = Depends(require_admin),
    ) -> JSONResponse:
        """Preview explicitly attributable verification data.

        With no ``run_id`` the endpoint lists all eligible provenance runs for
        the Settings panel.  Supplying a run UUID performs a bounded,
        run-specific graph preview and returns the digest required by cleanup.
        """

        selector: VerificationDataSelector | None = None
        if run_id is not None:
            # Validate opaque selector input before opening a database
            # session; malformed operator input should be a deterministic 400
            # even when the database itself is unavailable.
            try:
                selector = VerificationDataSelector(type=selector_type, id=run_id)
            except Exception as exc:
                raise _service_error_to_http(exc) from exc
        session = await _open_session(server)
        try:
            coordinator = _coordinator_for(server)
            actor = await _actor_id(server, request)
            if run_id is None:
                result = await _list_previews(coordinator, session, actor_user_id=actor)
                _coordinator_type, _error_type, load_manifest = _load_cleanup_types()
                result = await _append_legacy_manifest_previews(
                    coordinator,
                    session,
                    result,
                    actor_user_id=actor,
                    load_manifest=load_manifest,
                )
            else:
                _coordinator_type, _error_type, load_manifest = _load_cleanup_types()
                # Lightweight route test doubles (and staged deployments
                # before the ledger migration) may expose only commit/close;
                # do not issue a real ledger query against those adapters.
                provenance = _provenance_service_for_session(session)
                assert selector is not None
                result = await _preview_selector(
                    coordinator,
                    session,
                    selector,
                    provenance=provenance,
                    load_manifest=load_manifest,
                    actor_user_id=actor,
                )
                result.setdefault("selector", {"type": selector.type, "id": selector.id})
                result.setdefault("preview_digest", result.get("digest") or _canonical_digest(result))
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Verification data preview failed", exc_info=True)
            raise _service_error_to_http(exc) from exc
        finally:
            await _close_session(session)

    @app.post("/api/admin/verification-data/cleanup")
    async def cleanup_verification_data(
        payload: VerificationDataCleanupPayload,
        request: Request,
        _: None = Depends(require_admin),
    ) -> JSONResponse:
        """Delete only explicitly attributable verification/test data."""

        session = await _open_session(server)
        try:
            coordinator = _coordinator_for(server)
            actor = await _actor_id(server, request)
            _coordinator_type, _error_type, load_manifest = _load_cleanup_types()
            provenance = _provenance_service_for_session(session)

            # The UI sends the digest for the *complete* server-side preview,
            # while intentionally submitting only selectors classified A.  A
            # fresh full listing is therefore the first TOCTOU check.  Never
            # derive the expected digest from the client-provided subset.
            full_preview = await _list_previews(
                coordinator,
                session,
                actor_user_id=actor,
            )
            full_preview = await _append_legacy_manifest_previews(
                coordinator,
                session,
                full_preview,
                actor_user_id=actor,
                load_manifest=load_manifest,
            )
            expected_listing_digest = _preview_listing_digest(full_preview)
            if payload.preview_digest != expected_listing_digest:
                raise HTTPException(
                    status_code=409,
                    detail="Preview is stale; refresh verification data and try again",
                )
            eligible_a = _eligible_a_selector_keys(full_preview)

            # Re-read every requested selector immediately before deletion
            # (second TOCTOU check).  The aggregate digest is deterministic and
            # covers selector identity plus every service-produced graph
            # projection, but does not grant B/C selectors authority.
            previews: list[dict[str, Any]] = []
            manifests: list[Any] = []
            for selector in payload.selectors:
                key = (selector.type, selector.id)
                if key not in eligible_a:
                    raise HTTPException(
                        status_code=409,
                        detail="Only explicitly classified A verification data may be deleted",
                    )
                manifest = await _resolve_manifest(
                    session,
                    selector,
                    provenance=provenance,
                    load_manifest=load_manifest,
                )
                manifests.append(manifest)
                preview = await _preview_selector(
                    coordinator,
                    session,
                    selector,
                    provenance=provenance,
                    load_manifest=load_manifest,
                    actor_user_id=actor,
                    manifest=manifest,
                )
                if preview.get("eligible") is False:
                    raise HTTPException(
                        status_code=409,
                        detail="Verification selector is no longer eligible",
                    )
                # Some listing implementations include a per-selector digest
                # (legacy manifests do); when present, compare it with this
                # fresh graph preview before allowing execution.  UUID-run
                # listings may only expose the aggregate digest, in which
                # case the coordinator's own execute-time digest fence below
                # remains authoritative.
                listed_selector_digest: str | None = None
                for listed in full_preview.get("selectors", []):
                    if _selector_from_value(listed) != key or not isinstance(listed, Mapping):
                        continue
                    candidate_digest = listed.get("preview_digest") or listed.get("digest")
                    if isinstance(candidate_digest, str) and candidate_digest.strip():
                        listed_selector_digest = candidate_digest.strip()
                    break
                fresh_digest = preview.get("digest") or preview.get("preview_digest")
                if (
                    listed_selector_digest is not None
                    and isinstance(fresh_digest, str)
                    and fresh_digest.strip()
                    and fresh_digest.strip() != listed_selector_digest
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Verification preview changed; refresh and try again",
                    )
                previews.append(
                    {
                        "selector": {"type": selector.type, "id": selector.id},
                        "preview": preview,
                    }
                )

            deleted: list[dict[str, Any]] = []
            for selector, manifest, item in zip(payload.selectors, manifests, previews):
                preview = item["preview"]
                confirmation_digest = str(
                    preview.get("digest")
                    or preview.get("preview_digest")
                    or expected_listing_digest
                )
                try:
                    # A cancelled HTTP request must not orphan a half-finished
                    # domain purge.  Let the coordinator finish/rollback its
                    # transaction before propagating cancellation.
                    result = await await_task_completion_before_cancellation(
                        _execute_selector(
                            coordinator,
                            session,
                            selector=selector,
                            manifest=manifest,
                            actor_user_id=actor,
                            confirmation_digest=confirmation_digest,
                        )
                    )
                except Exception as exc:
                    raise _service_error_to_http(exc) from exc
                if not isinstance(result, Mapping):
                    raise HTTPException(status_code=500, detail="Invalid verification cleanup response")
                deleted.append(dict(result))
            if callable(getattr(session, "commit", None)):
                await _maybe_await(session.commit())
            return JSONResponse(
                {
                    "status": "completed",
                    "selectors": [s.model_dump() for s in payload.selectors],
                    "preview_digest": expected_listing_digest,
                    "deleted": deleted,
                    "counts": {
                        "selectors": len(payload.selectors),
                        "runs": len(deleted),
                    },
                },
                headers={"Cache-Control": "no-store"},
            )
        except HTTPException:
            # Roll back here but let the single ``finally`` block own close;
            # closing twice breaks a few async session adapters and obscures
            # rollback/close ordering in operator audit tests.
            await _close_session(session, rollback=True, close=False)
            raise
        except Exception as exc:
            await _close_session(session, rollback=True, close=False)
            logger.error("Verification data cleanup failed", exc_info=True)
            raise _service_error_to_http(exc) from exc
        finally:
            await _close_session(session)
