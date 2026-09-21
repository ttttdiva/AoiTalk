"""System-owned routing for privacy-aware Document and App workflows.

This module is intentionally small.  It only decides *which* deterministic
workflow owns a turn and delegates all work to ``DocumentWorkflow`` or
``AppBuildWorkflow``.  Provider selection, privacy policy, and egress remain
owned by :mod:`cloud_advisor_service` and :mod:`outbound_privacy_service`.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


class WorkflowKind(str, Enum):
    DOCUMENT = "document"
    APP = "app"


@dataclass(frozen=True)
class WorkflowRoute:
    """Trusted routing decision for one turn.

    ``command`` is always a canonical slash token (or ``None`` for automatic
    routing).  No user supplied alias is carried into a workflow.
    """

    kind: WorkflowKind
    command: str | None = None
    automatic: bool = False
    intent: str = ""

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "workflow": self.kind.value,
            "command": self.command or "",
            "automatic": self.automatic,
            "intent": self.intent,
        }


_COMMANDS: dict[str, tuple[WorkflowKind, str]] = {
    "/document": (WorkflowKind.DOCUMENT, "document"),
    "/template": (WorkflowKind.DOCUMENT, "template"),
    "/app": (WorkflowKind.APP, "app"),
    "/macro": (WorkflowKind.APP, "macro"),
}


def parse_workflow_command(message: Any) -> WorkflowRoute | None:
    """Parse an exact leading system command.

    Matching is case-insensitive and accepts whitespace after the token, but
    never accepts a prefix such as ``/documentary``.  Attachments and command
    arguments are intentionally not parsed here; they remain untrusted input
    for the selected workflow.
    """

    if not isinstance(message, str):
        return None
    parts = message.strip().split(None, 1)
    if not parts:
        return None
    command = parts[0].casefold()
    selected = _COMMANDS.get(command)
    if selected is None:
        return None
    kind, intent = selected
    return WorkflowRoute(kind=kind, command=command, automatic=False, intent=intent)


def is_workflow_command(message: Any) -> bool:
    return parse_workflow_command(message) is not None


_DOCUMENT_WORDS = (
    "手順書",
    "テンプレート",
    "資料",
    "文書",
    "ドキュメント",
    "xlsx",
    "excel",
    "spreadsheet",
    "document",
    "template",
    "procedure",
)
_DOCUMENT_ACTIONS = (
    "作って",
    "作成",
    "更新",
    "改訂",
    "適用",
    "反映",
    "変換",
    "generate",
    "create",
    "update",
    "adapt",
)
_APP_WORDS = (
    "マクロ",
    "macro",
    "app",
    "アプリ",
    "判定",
    "ログを読",
    "config",
    "設定を読",
    "正常なら",
    "異常なら",
    "ok、",
    "ng",
)
_APP_ACTIONS = (
    "作って",
    "作成",
    "実装",
    "生成",
    "create",
    "build",
    "make",
)


def _has_any(text: str, values: Sequence[str]) -> bool:
    folded = text.casefold()
    for value in values:
        token = str(value).casefold()
        if re.fullmatch(r"[a-z0-9]+", token):
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded):
                return True
        elif token in folded:
            return True
    return False


def detect_workflow_intent(
    message: Any,
    attachments: Sequence[Mapping[str, Any]] | None = None,
) -> WorkflowRoute | None:
    """Conservative automatic intent routing.

    The detector is a narrow local preflight, not a model-controlled tool
    argument and not a second Cloud policy system.  It requires both domain
    vocabulary and an action phrase; ambiguous ordinary chat remains on the
    normal Main-model path.
    """

    if not isinstance(message, str):
        return None
    if parse_workflow_command(message) is not None:
        return parse_workflow_command(message)
    text = message.strip()
    if not text:
        return None
    has_attachments = bool(attachments)
    explicit_app_kind = _has_any(text, ("マクロ", "macro", "app", "アプリ"))
    if (
        _has_any(text, _APP_WORDS)
        and _has_any(text, _APP_ACTIONS)
        and (has_attachments or explicit_app_kind)
    ):
        return WorkflowRoute(
            kind=WorkflowKind.APP,
            automatic=True,
            intent="automatic_app_macro",
        )
    if _has_any(text, _DOCUMENT_WORDS) and _has_any(text, _DOCUMENT_ACTIONS):
        # A document turn without an attachment can still be useful when a
        # prior/current file is available through a workflow adapter.  Keep
        # this branch conservative for plain prose by requiring an Office
        # attachment unless the request explicitly says document/template.
        explicit = _has_any(text, ("手順書", "テンプレート", "document", "template"))
        if has_attachments or explicit:
            return WorkflowRoute(
                kind=WorkflowKind.DOCUMENT,
                automatic=True,
                intent="automatic_document",
            )
    return None


def workflow_route_for_turn(
    message: Any,
    attachments: Sequence[Mapping[str, Any]] | None = None,
) -> WorkflowRoute | None:
    """Resolve explicit commands first, then conservative automatic intent."""

    explicit = parse_workflow_command(message)
    return explicit or detect_workflow_intent(message, attachments)


def _authorized_workflow_attachments(
    attachments: Sequence[Mapping[str, Any]] | None,
    *,
    project_id: str | None,
    user_id: str | None = None,
    verified_paths: Sequence[str] | None = None,
) -> list[Mapping[str, Any]]:
    """Keep only inline data or project-scoped attachment references.

    Workflow callers receive chat metadata that is user-controlled.  A bare
    filesystem path must never be resolved against the global workspace (or a
    different user's directory); only the authenticated Project prefix is an
    accepted path authority.  Pathless inline content remains valid evidence
    supplied directly by the caller.
    """

    result: list[Mapping[str, Any]] = []
    project_prefix = (
        f"_projects/project_{str(project_id).strip()}/".casefold()
        if project_id
        else ""
    )
    user_prefix = (
        f"_users/user_{str(user_id).strip()}/".casefold()
        if user_id and str(user_id).strip() != "default_user"
        else ""
    )
    verified = {
        str(path).replace("\\", "/").strip().casefold()
        for path in (verified_paths or ())
        if isinstance(path, str) and path.strip()
    }
    path_keys = ("path", "file_path", "local_path", "resolved_path", "project_relative_path")
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        path_key = next(
            (key for key in path_keys if isinstance(item.get(key), str) and item.get(key).strip()),
            None,
        )
        if path_key is not None:
            scope_prefix = project_prefix or user_prefix
            if not scope_prefix:
                continue
            normalized = str(item[path_key]).replace("\\", "/").strip()
            parts = normalized.split("/")
            if (
                any(part in {"", ".", ".."} for part in parts)
                or not normalized.casefold().startswith(scope_prefix)
                or (verified and normalized.casefold() not in verified)
            ):
                continue
            if verified_paths is not None and normalized.casefold() not in verified:
                continue
            copy = dict(item)
            # Normalize to one server-recognized project-relative key.  Do
            # not retain alternate caller-controlled path fields.
            copy["path"] = normalized
            for key in path_keys:
                if key != "path":
                    copy.pop(key, None)
            result.append(copy)
            continue
        # Inline content is caller-provided data, not a filesystem authority.
        # Ignore data URLs and arbitrary metadata because the workflow
        # adapters do not decode them as a bounded local file.
        if any(isinstance(item.get(key), (str, bytes, bytearray)) for key in ("content", "text", "data", "bytes")):
            result.append(item)
        else:
            # Preserve metadata-only items for compatibility with embedding
            # fakes; they cannot resolve to a local path without an explicit
            # project-scoped path and therefore remain non-readable.
            result.append(item)
    return result


def _contains_attachment_path(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    return any(
        isinstance(item.get(key), str) and item.get(key).strip()
        for key in ("path", "file_path", "local_path", "resolved_path", "project_relative_path")
    )


async def create_persistent_app_context(
    *,
    user_id: str,
    project_id: str | None = None,
    name: str = "AoiTalk App",
    slug: str = "aoitalk-workflow-app",
    description: str = "",
    workspace_root: str | None = None,
) -> Mapping[str, Any]:
    """Create an App through the existing Apps domain transaction.

    This adapter is intentionally opt-in.  It is used by the Chat workflow
    only after the request boundary has authenticated the user; Project
    access is checked again here so a direct embedding cannot create a
    binding outside the caller's write scope.
    """

    from uuid import UUID

    from ..memory.database import get_database_manager
    from ..memory.models import ProjectApp
    from .app_service import AppService
    from .app_storage import ensure_app_instance, get_app_workspace_path

    try:
        owner = UUID(str(user_id))
    except (TypeError, ValueError) as exc:
        raise PermissionError("authenticated user identity is required") from exc
    project_uuid = None
    if project_id:
        try:
            project_uuid = UUID(str(project_id))
        except (TypeError, ValueError) as exc:
            raise PermissionError("project identity is invalid") from exc
    manager = get_database_manager()
    session = await manager.get_session()
    service = AppService(workspace_root=workspace_root)
    app = None
    committed = False
    try:
        if project_uuid is not None and not await service.project_write_access(
            session,
            project_id=project_uuid,
            user_id=owner,
        ):
            raise PermissionError("project write access is required")
        # The caller owns the visible request text; use a bounded stable name
        # supplied by the controller rather than persisting raw source data.
        safe_name = re.sub(r"[\r\n]+", " ", str(name or "AoiTalk App")).strip()[:255] or "AoiTalk App"
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(slug or "aoitalk-workflow-app")).strip("-")[:100] or "aoitalk-workflow-app"
        app = await service.create_app(
            session,
            owner_user_id=owner,
            name=safe_name,
            slug=safe_slug,
            description=str(description or "")[:2_000],
            origin_project_id=project_uuid,
            visibility="private",
        )
        if project_uuid is not None:
            session.add(
                ProjectApp(
                    project_id=project_uuid,
                    app_id=app.id,
                    binding_mode="development",
                    created_by=owner,
                )
            )
            ensure_app_instance(
                project_uuid,
                app.id,
                workspace_root=workspace_root,
            )
        await session.commit()
        committed = True
        return {
            "app": app,
            "app_id": str(app.id),
            "workspace": str(
                get_app_workspace_path(app.id, workspace_root=workspace_root)
            ),
        }
    except Exception:
        if not committed:
            await session.rollback()
        raise
    finally:
        await session.close()


def _result_message(result: Any) -> str:
    if isinstance(result, Mapping):
        for key in ("user_message", "message", "assistant_content", "summary"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("user_message", "message", "assistant_content", "summary"):
        value = getattr(result, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "ワークフローを完了しました。"


class WorkflowController:
    """Dispatch one user turn to the selected system workflow."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        document_workflow: Any | None = None,
        app_workflow: Any | None = None,
        app_service: Any | None = None,
        app_context_callback: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.document_workflow = document_workflow
        self.app_workflow = app_workflow
        self.app_service = app_service
        self.app_context_callback = app_context_callback

    async def _workflow_instance(
        self,
        route: WorkflowRoute,
        *,
        workspace_root: str | None = None,
    ) -> Any:
        if route.kind is WorkflowKind.DOCUMENT:
            if self.document_workflow is None:
                from .document_workflow import DocumentWorkflow

                # Do not cache the default instance: workflow services own
                # ephemeral protected mappings and must never be shared by
                # concurrent turns.  An explicitly injected fake/service is
                # retained for embedding/test seams.
                return DocumentWorkflow(config=self.config)
            return self.document_workflow
        if self.app_workflow is None:
            from .app_workflow import AppBuildWorkflow

            return AppBuildWorkflow(
                config=self.config,
                app_service=self.app_service,
                workspace_root=workspace_root,
                app_context_callback=self.app_context_callback,
            )
        return self.app_workflow

    async def execute(
        self,
        message: str,
        *,
        attachments: Sequence[Mapping[str, Any]] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        main_model: Any | None = None,
        output_dir: str | None = None,
        workspace_root: str | None = None,
        progress_callback: Callable[[str, Mapping[str, Any]], Awaitable[Any] | Any]
        | None = None,
        route: WorkflowRoute | None = None,
        verified_attachment_paths: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Any | None:
        selected = route or workflow_route_for_turn(message, attachments)
        if selected is None:
            return None
        workflow = await self._workflow_instance(
            selected,
            workspace_root=workspace_root,
        )
        execute = getattr(workflow, "execute", None)
        if not callable(execute):
            raise RuntimeError("workflow execution entrypoint is unavailable")
        call_kwargs = {
            "session_id": session_id,
            "user_id": user_id,
            "project_id": project_id,
            "main_model": main_model,
            "output_dir": output_dir,
            "workspace_root": workspace_root,
            "progress_callback": progress_callback,
            **kwargs,
        }
        if selected.kind is WorkflowKind.DOCUMENT:
            # DocumentWorkflow accepts the chat-shaped arguments and performs
            # its own bounded attachment classification/rebinding.
            allowed_roots: list[str] = []
            if workspace_root:
                root = Path(workspace_root)
                if project_id:
                    allowed_roots.append(str(root / f"_projects/project_{project_id}"))
                elif user_id and user_id != "default_user":
                    allowed_roots.append(str(root / f"_users/user_{user_id}"))
            workflow_attachments = _authorized_workflow_attachments(
                attachments,
                project_id=project_id,
                user_id=user_id,
                verified_paths=verified_attachment_paths,
            )
            raw_path_count = sum(_contains_attachment_path(item) for item in attachments or ())
            filtered_path_count = sum(_contains_attachment_path(item) for item in workflow_attachments)
            if raw_path_count != filtered_path_count:
                raise PermissionError("workflow attachment authorization failed")
            document_kwargs: dict[str, Any] = {
                    "message": message,
                    "attachments": workflow_attachments,
                    "command": selected.command,
                    "intent": selected.intent,
                    "allowed_roots": allowed_roots or None,
            }
            # If no separate notes attachment exists, labelled values in the
            # local chat request may serve as current-project notes.  The
            # DocumentWorkflow masks them before Cloud projection.
            if not any(
                str(item.get("name") if isinstance(item, Mapping) else item)
                .casefold()
                .endswith((".txt", ".md", ".csv", ".log"))
                for item in workflow_attachments
            ):
                document_kwargs["current_notes"] = message
            call_kwargs.update(document_kwargs)
        else:
            # AppBuildWorkflow deliberately uses ``request``/``inputs`` names
            # so raw source is clearly distinguished from the public chat
            # message.  It still receives the canonical slash intent.
            allowed_roots = []
            if workspace_root:
                root = Path(workspace_root)
                if project_id:
                    allowed_roots.append(str(root / f"_projects/project_{project_id}"))
                elif user_id and user_id != "default_user":
                    allowed_roots.append(str(root / f"_users/user_{user_id}"))
            workflow_attachments = _authorized_workflow_attachments(
                attachments,
                project_id=project_id,
                user_id=user_id,
                verified_paths=verified_attachment_paths,
            )
            raw_path_count = sum(_contains_attachment_path(item) for item in attachments or ())
            filtered_path_count = sum(_contains_attachment_path(item) for item in workflow_attachments)
            if raw_path_count != filtered_path_count:
                raise PermissionError("workflow attachment authorization failed")
            call_kwargs.update(
                {
                    "request": message,
                    "kind": selected.command or "app",
                    "inputs": workflow_attachments,
                    "allowed_roots": allowed_roots or None,
                }
            )
        # Keep compatibility with small embedding workflows that expose a
        # narrower signature.  We inspect instead of catching TypeError from
        # inside the workflow, so implementation errors are not retried.
        try:
            signature = inspect.signature(execute)
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if not accepts_kwargs:
                call_kwargs = {
                    key: value
                    for key, value in call_kwargs.items()
                    if key in signature.parameters
                }
        except (TypeError, ValueError):
            pass
        # The terminal request path already binds TurnContext, but the
        # controller is also a reusable backend entrypoint for HTTP/CLI
        # callers.  Bind the supplied authenticated identity when the caller
        # did not establish an equivalent context; Cloud Advisor's budget and
        # parent authority then remain request-scoped instead of silently
        # falling back to an invalid/missing turn.
        turn_token = None
        try:
            from .turn_context import get_turn_context, set_turn_context, reset_turn_context

            current_turn = get_turn_context()
            current_identity = (
                str(getattr(current_turn, "user_id", None) or ""),
                str(getattr(current_turn, "session_id", None) or ""),
                str(getattr(current_turn, "project_id", None) or ""),
            )
            requested_identity = (
                str(user_id or ""),
                str(session_id or ""),
                str(project_id or ""),
            )
            if any(requested_identity) and current_identity != requested_identity:
                turn_token = set_turn_context(
                    user_id=user_id,
                    session_id=session_id,
                    project_id=project_id,
                    client_message_id=kwargs.get("client_message_id"),
                )
        except Exception:
            turn_token = None
        try:
            result = execute(**call_kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            if turn_token is not None:
                try:
                    reset_turn_context(turn_token)
                except Exception:
                    pass

    async def handle(self, *args: Any, **kwargs: Any) -> str | None:
        result = await self.execute(*args, **kwargs)
        return None if result is None else _result_message(result)


__all__ = [
    "WorkflowController",
    "WorkflowKind",
    "WorkflowRoute",
    "detect_workflow_intent",
    "is_workflow_command",
    "parse_workflow_command",
    "workflow_route_for_turn",
]
