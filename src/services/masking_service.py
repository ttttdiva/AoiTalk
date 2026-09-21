"""Trusted built-in ``/masking`` service.

The slash command is intercepted by the backend before ordinary model
dispatch.  This service never calls a main/cloud LLM and does not read or
rewrite conversation history.  It creates one invocation-scoped
``PrivacyMaskingBoundary`` (therefore one in-memory alias scope), transforms
optional text and authorized files, and invokes an optional persistence
callback with a safe result projection only.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .masking_file_transformer import (
    MaskedFileResult,
    MaskingFileTransformer,
)
from .privacy_masking_boundary import (
    MaskedProjection,
    PrivacyMaskingBoundary,
    PrivacyMaskingError,
)

MASKING_COMMAND = "/masking"
MASKING_COMMAND_NAME = "masking"
MASKING_SOURCE_METADATA_KEY = "privacy_masking"
MASKING_SOURCE_METADATA = {MASKING_SOURCE_METADATA_KEY: {"source": True}}
MASKING_RESULT_METADATA = {MASKING_SOURCE_METADATA_KEY: {"result": True}}


class MaskingValidationError(PrivacyMaskingError):
    """The command contains neither usable text nor authorized files."""


class MaskingPersistenceError(PrivacyMaskingError):
    """The caller's optional persistence callback failed."""


@dataclass(frozen=True)
class MaskingCommand:
    """Canonical parser output for one direct ``/masking`` message."""

    token: str
    input_text: str = field(default="", repr=False)

    @property
    def command(self) -> str:
        return MASKING_COMMAND

    @property
    def text(self) -> str:
        return self.input_text

    @property
    def args(self) -> str:
        return self.input_text

    @property
    def name(self) -> str:
        return MASKING_COMMAND_NAME


def parse_masking_command(message: Any) -> MaskingCommand | None:
    """Parse exactly one leading canonical ``/masking`` token.

    Leading whitespace is accepted for parity with the existing slash parser;
    a token prefix such as ``/masking-extra`` is rejected.  Matching is
    case-insensitive while the returned token is always canonical, so menu
    selection and directly typed commands share one execution path.
    """

    if not isinstance(message, str):
        return None
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(None, 1)
    if not parts or parts[0][1:].casefold() != MASKING_COMMAND_NAME:
        return None
    text = parts[1].strip() if len(parts) > 1 else ""
    return MaskingCommand(token=MASKING_COMMAND, input_text=text)


# Alternate names used by integrations/tests.
parse_masking_token = parse_masking_command
parse_masking_slash = parse_masking_command


@dataclass(frozen=True)
class MaskingResult:
    """Safe output of one masking invocation.

    ``masked_text`` and generated file metadata contain only masked content.
    Source identifiers are included for backend correlation but raw source
    payload, source paths, and alias maps are deliberately absent.
    """

    masked_text: str | None = field(default=None, repr=False)
    files: tuple[MaskedFileResult, ...] = ()
    findings: tuple[Any, ...] = ()
    semantic_status: str = "disabled"
    status: str = "success"
    invocation_id: str = ""
    source_message_id: str = field(default="", repr=False)
    session_id: str = field(default="", repr=False)
    actor_id: str = field(default="", repr=False)
    project_id: str = field(default="", repr=False)
    # A service result is a safe assistant/output projection.  The request
    # boundary writes ``MASKING_SOURCE_METADATA`` beside the original raw user
    # row separately; this marker therefore identifies the result and must not
    # make downstream projections hide the masked assistant output as if it
    # were raw source material.
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: {MASKING_SOURCE_METADATA_KEY: {"result": True}},
        repr=False,
    )

    @property
    def ok(self) -> bool:
        return bool(self.masked_text is not None or self.files)

    @property
    def masked_files(self) -> tuple[MaskedFileResult, ...]:
        return self.files

    @property
    def output_files(self) -> tuple[MaskedFileResult, ...]:
        return self.files

    @property
    def generated_files(self) -> tuple[MaskedFileResult, ...]:
        return self.files

    @property
    def assistant_content(self) -> str:
        """Safe assistant content with no source path/filename disclosure."""

        chunks: list[str] = []
        if self.masked_text is not None:
            chunks.append(self.masked_text)
        if self.files:
            count = len(self.files)
            chunks.append(f"マスキング済みファイルを{count}件作成しました。")
        return "\n\n".join(chunks)

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize only fields safe for assistant/result metadata."""

        return {
            "masked_text": self.masked_text,
            "files": [file.to_public_dict() for file in self.files],
            "semantic_status": self.semantic_status,
            "status": self.status,
            "invocation_id": self.invocation_id,
            "metadata": {MASKING_SOURCE_METADATA_KEY: {"result": True}},
        }

    as_dict = to_public_dict

    def to_persistence_payload(self) -> dict[str, Any]:
        """Return callback payload without raw source/path material."""

        payload = self.to_public_dict()
        payload.update(
            {
                "source_message_id": self.source_message_id,
                "session_id": self.session_id,
                "actor_id": self.actor_id,
                "project_id": self.project_id,
            }
        )
        return payload


def _safe_id(value: Any) -> str:
    return str(value or "").strip()


def _findings_tuple(items: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for finding in items:
        category = str(getattr(finding, "category", "") or "")
        placeholder = str(getattr(finding, "placeholder", "") or "")
        key = (category, placeholder)
        if key in seen and key != ("", ""):
            continue
        if key != ("", ""):
            seen.add(key)
        result.append(finding)
    return tuple(result)


def _semantic_status(values: Iterable[str]) -> str:
    normalized = [str(value or "disabled").strip().casefold() for value in values]
    if any(value in {"failed", "error", "unavailable"} for value in normalized):
        return "failed"
    if "success" in normalized:
        return "success"
    if "cached" in normalized:
        return "cached"
    return "disabled"


async def _invoke_callback(callback: Callable[..., Any], payload: Any) -> Any:
    try:
        result = callback(payload)
    except TypeError:
        # An embedding may declare a keyword-only callback.  Keep this narrow
        # compatibility fallback; callback-internal TypeError is not retried.
        try:
            result = callback(result=payload)
        except TypeError:
            raise
    if inspect.isawaitable(result):
        return await result
    return result


class PrivacyMaskingService:
    """Canonical local masking service used by backend slash interception."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        semantic_redactor: Callable[..., Any] | None = None,
        workspace_root: str | None = None,
        authorized_roots: Iterable[str] = (),
        attachment_authorizer: Callable[..., Any] | None = None,
        gateway_factory: Callable[..., Any] | None = None,
        persistence_callback: Callable[..., Any] | None = None,
        persist: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.semantic_redactor = semantic_redactor
        self.workspace_root = workspace_root
        self.authorized_roots = tuple(authorized_roots)
        self.attachment_authorizer = attachment_authorizer
        self.gateway_factory = gateway_factory
        self.persistence_callback = persistence_callback or persist

    def _new_boundary(self) -> PrivacyMaskingBoundary:
        kwargs: dict[str, Any] = {"semantic_redactor": self.semantic_redactor}
        if self.gateway_factory is not None:
            kwargs["gateway_factory"] = self.gateway_factory
        return PrivacyMaskingBoundary(self.config, **kwargs)

    @staticmethod
    def _validate_ids(*, source_message_id: Any = None, session_id: Any = None, actor_id: Any = None, project_id: Any = None) -> tuple[str, str, str, str]:
        return (
            _safe_id(source_message_id),
            _safe_id(session_id),
            _safe_id(actor_id),
            _safe_id(project_id),
        )

    async def execute_async(
        self,
        text: str | None = None,
        attachments: Iterable[Any] | None = None,
        *,
        source_message_id: Any = None,
        message_id: Any = None,
        session_id: Any = None,
        actor_id: Any = None,
        user_id: Any = None,
        project_id: Any = None,
        invocation_id: Any = None,
        persistence_callback: Callable[..., Any] | None = None,
        persist: Callable[..., Any] | None = None,
    ) -> MaskingResult:
        """Process text and/or files in one fresh one-way privacy scope."""

        source_message_id, session_id, actor_id, project_id = self._validate_ids(
            source_message_id=source_message_id or message_id,
            session_id=session_id,
            actor_id=actor_id or user_id,
            project_id=project_id,
        )
        command = parse_masking_command(text) if isinstance(text, str) else None
        input_text: str | None
        if command is not None:
            input_text = command.input_text
        else:
            input_text = text if isinstance(text, str) else None
        source_files = list(attachments or ())
        if not (input_text and input_text.strip()) and not source_files:
            raise MaskingValidationError("masking requires text or an authorized file")

        active_invocation = _safe_id(invocation_id) or uuid.uuid4().hex
        boundary = self._new_boundary()
        statuses: list[str] = []
        findings: list[Any] = []
        masked_text: str | None = None
        if input_text is not None and input_text.strip():
            projection = await boundary.mask_text(input_text, source_kind="masking_text")
            masked_text = projection.value
            findings.extend(projection.findings)
            statuses.append(projection.semantic_status)

        files: tuple[MaskedFileResult, ...] = ()
        if source_files:
            transformer = MaskingFileTransformer(
                workspace_root=self.workspace_root,
                user_id=actor_id,
                project_id=project_id,
                authorized_roots=self.authorized_roots,
                authorization_callback=self.attachment_authorizer,
            )
            files, file_findings, file_status = await transformer.transform_async(
                source_files,
                masker=boundary.mask_text,
            )
            findings.extend(file_findings)
            statuses.append(file_status)

        result = MaskingResult(
            masked_text=masked_text,
            files=files,
            findings=_findings_tuple(findings),
            semantic_status=_semantic_status(statuses),
            invocation_id=active_invocation,
            source_message_id=source_message_id,
            session_id=session_id,
            actor_id=actor_id,
            project_id=project_id,
            metadata={MASKING_SOURCE_METADATA_KEY: {"result": True}},
        )
        callback = persistence_callback or persist or self.persistence_callback
        if callback is not None:
            try:
                await _invoke_callback(callback, result.to_persistence_payload())
            except Exception as exc:  # noqa: BLE001
                raise MaskingPersistenceError("masking result persistence failed") from exc
        return result

    async def mask_async(self, *args: Any, **kwargs: Any) -> MaskingResult:
        return await self.execute_async(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> MaskingResult:
        """Synchronous entrypoint compatible with CLI/tool callers."""

        coroutine = self.execute_async(*args, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        import concurrent.futures
        import contextvars

        future: concurrent.futures.Future[MaskingResult] = concurrent.futures.Future()
        context = contextvars.copy_context()

        def run() -> None:
            try:
                future.set_result(context.run(asyncio.run, coroutine))
            except BaseException as exc:  # noqa: BLE001
                try:
                    future.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass

        import threading

        threading.Thread(target=run, name="aoitalk-masking-service", daemon=True).start()
        try:
            return future.result(timeout=180.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise PrivacyMaskingError("privacy masking timed out") from exc

    mask = execute
    run = execute


# Historical/short aliases make the service easy to discover without creating
# a second implementation or another slash command.
MaskingService = PrivacyMaskingService
PrivacyMaskingResult = MaskingResult


__all__ = [
    "MASKING_COMMAND",
    "MASKING_COMMAND_NAME",
    "MASKING_RESULT_METADATA",
    "MASKING_SOURCE_METADATA",
    "MaskingCommand",
    "MaskingPersistenceError",
    "MaskingResult",
    "MaskingService",
    "MaskingValidationError",
    "PrivacyMaskingResult",
    "PrivacyMaskingService",
    "parse_masking_command",
    "parse_masking_slash",
    "parse_masking_token",
]
