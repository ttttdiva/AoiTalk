"""
LLM tool permission manager.

Manages user permission requests for LLM initiated actions such as external
search, file writes/deletes, and command execution. When the active generation
policy requires confirmation, a request is sent to the WebUI and execution waits
for the user's approve/deny response.
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum

from ..llm.generation_policy import PermissionPolicy, get_current_generation_policy

logger = logging.getLogger(__name__)


class PermissionStatus(Enum):
    """Permission request status"""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass
class PermissionRequest:
    """Represents a pending permission request"""
    request_id: str
    tool_name: str
    tool_args: Dict[str, Any]
    description: str
    status: PermissionStatus = PermissionStatus.PENDING
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)


FILE_WRITE_TOOLS = {
    "create_file",
    "append_to_file",
    "edit_file",
    "insert_to_file",
    "undo_edit",
    "create_workspace_directory",
    "upload_workspace_file",
    "move_workspace_item",
    "upload_user_file",
}

FILE_DELETE_TOOLS = {
    "delete_file",
    "delete_workspace_item",
    "delete_user_file",
}

COMMAND_TOOLS = {"execute_command"}

EXTERNAL_SEARCH_TOOLS = {"web_search", "grok_x_search"}

DEFAULT_PERMISSION_TOOLS = sorted(
    EXTERNAL_SEARCH_TOOLS | FILE_WRITE_TOOLS | FILE_DELETE_TOOLS | COMMAND_TOOLS
)


class ExternalLLMPermissionManager:
    """
    Manages permission requests for external LLM API calls.
    
    When auto_approve is False, the manager will:
    1. Send a permission request to the WebUI via broadcast callback
    2. Wait for user response (approve/deny)
    3. Return the decision to the caller
    """
    
    def __init__(self, config=None):
        """
        Initialize the permission manager.
        
        Args:
            config: Application config object or dict
        """
        self.config = config
        self._pending_requests: Dict[str, PermissionRequest] = {}
        self._broadcast_callback: Optional[Callable] = None
        self._timeout_seconds = 300  # 5 minutes timeout
        
        # Load config
        self._load_config()
    
    def _load_config(self):
        """Load configuration settings"""
        self.auto_approve = True  # Default to current behavior outside agent modes
        self.enabled_tools = DEFAULT_PERMISSION_TOOLS.copy()
        
        if self.config is None:
            return
        
        # Get external_llm config
        external_llm_config = None
        if hasattr(self.config, 'get'):
            external_llm_config = self.config.get('external_llm', {})
        elif isinstance(self.config, dict):
            external_llm_config = self.config.get('external_llm', {})
        
        if external_llm_config:
            self.auto_approve = external_llm_config.get('auto_approve', True)
            self.enabled_tools = external_llm_config.get('tools', self.enabled_tools)
        
        logger.info(f"[ExternalLLMPermission] auto_approve={self.auto_approve}, tools={self.enabled_tools}")
    
    def set_broadcast_callback(self, callback: Callable):
        """
        Set the callback for broadcasting permission requests to WebUI.
        
        Args:
            callback: Async function that takes a message dict and broadcasts to clients
        """
        self._broadcast_callback = callback
    
    def is_permission_required(self, tool_name: str) -> bool:
        """
        Check if permission is required for the given tool.
        
        Args:
            tool_name: Name of the tool
            
        Returns:
            True if permission is required (auto_approve=False and tool is in list)
        """
        permission_policy = get_current_generation_policy().permission_policy
        if permission_policy == PermissionPolicy.AUTO_APPROVE:
            return False
        if permission_policy == PermissionPolicy.CONFIRM_MUTATIONS:
            return tool_name in (FILE_WRITE_TOOLS | FILE_DELETE_TOOLS | COMMAND_TOOLS)
        if permission_policy == PermissionPolicy.CONFIRM_ALL_TOOLS:
            return tool_name in self.enabled_tools

        if self.auto_approve:
            return False
        return tool_name in self.enabled_tools
    
    async def request_permission(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        description: str = ""
    ) -> bool:
        """
        Request permission from user for external LLM API call.
        
        Args:
            tool_name: Name of the tool
            tool_args: Arguments being passed to the tool
            description: Human-readable description of the action
            
        Returns:
            True if approved, False if denied or timeout
        """
        # Auto-approve if configured/current mode allows it.
        if not self.is_permission_required(tool_name):
            return True
        
        # Require broadcast callback
        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, denying")
            return False
        
        # Create request
        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        
        request = PermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description or self._generate_description(tool_name, tool_args),
            future=future,
            loop=loop,
        )
        
        self._pending_requests[request_id] = request
        
        # Broadcast permission request to WebUI
        try:
            await self._broadcast_callback({
                "type": "external_llm_permission_request",
                "data": {
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "description": request.description
                }
            })
            
            logger.info(f"[ExternalLLMPermission] Sent permission request: {request_id} for {tool_name}")
            
            # Wait for response with timeout
            try:
                result = await asyncio.wait_for(future, timeout=self._timeout_seconds)
                return result
            except asyncio.TimeoutError:
                logger.warning(f"[ExternalLLMPermission] Permission request timed out: {request_id}")
                request.status = PermissionStatus.TIMEOUT
                return False
                
        except Exception as e:
            logger.error(f"[ExternalLLMPermission] Error requesting permission: {e}")
            return False
        finally:
            # Clean up
            self._pending_requests.pop(request_id, None)

    async def request_external_model_prompt(
        self,
        prompt: str,
        *,
        redacted_prompt: str = "",
        redaction_findings: Optional[list[dict[str, str]]] = None,
        provider: str,
        model: str,
        description: str = "",
        confirm: bool = True,
        notify: bool = True,
        request_kind: str = "external_model_prompt",
    ) -> Optional[str]:
        """Ask the WebUI to approve or edit a prompt before an external model call."""
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        if not confirm:
            return outbound_prompt

        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, denying external model prompt")
            return None

        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        request = PermissionRequest(
            request_id=request_id,
            tool_name=request_kind,
            tool_args={
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "redacted_prompt": outbound_prompt,
            },
            description=description
            or f"分担先モデル {provider}/{model} へ送信するプロンプトを確認してください",
            future=future,
            loop=loop,
        )
        self._pending_requests[request_id] = request

        try:
            await self._broadcast_callback(
                {
                    "type": "external_model_prompt_request",
                    "data": {
                        "request_id": request_id,
                        "provider": provider,
                        "model": model,
                        "prompt": prompt,
                        "original_prompt": prompt,
                        "redacted_prompt": outbound_prompt,
                        "redaction_findings": redaction_findings or [],
                        "description": request.description,
                        "notify": notify,
                    },
                }
            )
            result = await asyncio.wait_for(future, timeout=self._timeout_seconds)
            if isinstance(result, dict) and result.get("approved"):
                edited_prompt = str(result.get("prompt") or "").strip()
                return edited_prompt or outbound_prompt
            return None
        except asyncio.TimeoutError:
            logger.warning("[ExternalLLMPermission] External model prompt request timed out: %s", request_id)
            request.status = PermissionStatus.TIMEOUT
            return None
        except Exception as e:
            logger.error("[ExternalLLMPermission] External model prompt request failed: %s", e)
            return None
        finally:
            self._pending_requests.pop(request_id, None)
    
    def handle_permission_response(self, request_id: str, approved: bool):
        """
        Handle user response to permission request.
        
        Args:
            request_id: The request ID
            approved: True if user approved, False if denied
        """
        request = self._pending_requests.get(request_id)
        if not request:
            logger.warning(f"[ExternalLLMPermission] Unknown request ID: {request_id}")
            return
        
        request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED
        
        if request.future and not request.future.done():
            if request.loop and request.loop.is_running():
                request.loop.call_soon_threadsafe(request.future.set_result, approved)
            else:
                request.future.set_result(approved)
        
        logger.info(f"[ExternalLLMPermission] Permission response: {request_id} -> {'approved' if approved else 'denied'}")

    def handle_external_model_prompt_response(
        self,
        request_id: str,
        approved: bool,
        prompt: str = "",
    ):
        """Handle user response for an external model prompt request."""
        request = self._pending_requests.get(request_id)
        if not request:
            logger.warning("[ExternalLLMPermission] Unknown external model prompt request ID: %s", request_id)
            return

        request.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED
        payload = {"approved": approved, "prompt": prompt}

        if request.future and not request.future.done():
            if request.loop and request.loop.is_running():
                request.loop.call_soon_threadsafe(request.future.set_result, payload)
            else:
                request.future.set_result(payload)

        logger.info(
            "[ExternalLLMPermission] External model prompt response: %s -> %s",
            request_id,
            "approved" if approved else "denied",
        )
    
    def _generate_description(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Generate a human-readable description of the action"""
        descriptions = {
            "web_search": lambda args: f"OpenAI APIによるWeb検索: 「{args.get('query', '')}」",
            "grok_x_search": lambda args: f"X (Twitter) 検索: 「{args.get('query', '')}」",
            "execute_command": lambda args: f"コマンド実行: {args.get('command', '')}",
            "create_file": lambda args: f"ファイル作成: {args.get('path', '')}",
            "append_to_file": lambda args: f"ファイル追記: {args.get('path', '')}",
            "edit_file": lambda args: f"ファイル編集: {args.get('path', '')}",
            "insert_to_file": lambda args: f"ファイル挿入: {args.get('path', '')}",
            "undo_edit": lambda args: f"ファイル編集の取り消し: {args.get('path', '')}",
            "delete_file": lambda args: f"ファイル/フォルダ削除: {args.get('path', '')}",
            "create_workspace_directory": lambda args: (
                f"ワークスペースフォルダ作成: {args.get('path', '')}/{args.get('name', '')}"
            ),
            "upload_workspace_file": lambda args: (
                f"ワークスペースファイル保存: {args.get('path', '')}/{args.get('filename', '')}"
            ),
            "delete_workspace_item": lambda args: f"ワークスペース項目削除: {args.get('path', '')}",
            "move_workspace_item": lambda args: (
                f"ワークスペース項目移動: {args.get('src', '')} -> {args.get('dest', '')}"
            ),
            "upload_user_file": lambda args: f"ユーザーファイル保存: {args.get('filename', '')}",
            "delete_user_file": lambda args: f"ユーザーファイル削除: {args.get('filename', '')}",
        }
        
        generator = descriptions.get(tool_name)
        if generator:
            return generator(tool_args)
        return f"{tool_name} を実行"


# Global instance (initialized by server)
_permission_manager: Optional[ExternalLLMPermissionManager] = None


def get_permission_manager() -> Optional[ExternalLLMPermissionManager]:
    """Get the global permission manager instance"""
    return _permission_manager


def set_permission_manager(manager: ExternalLLMPermissionManager):
    """Set the global permission manager instance"""
    global _permission_manager
    _permission_manager = manager


async def request_external_model_prompt(
    prompt: str,
    *,
    redacted_prompt: str = "",
    redaction_findings: Optional[list[dict[str, str]]] = None,
    provider: str,
    model: str,
    description: str = "",
    confirm: bool = True,
    notify: bool = True,
    request_kind: str = "external_model_prompt",
) -> Optional[str]:
    manager = get_permission_manager()
    if manager is None:
        outbound_prompt = (redacted_prompt or "").strip() or prompt
        return outbound_prompt if not confirm else None

    return await manager.request_external_model_prompt(
        prompt,
        redacted_prompt=redacted_prompt,
        redaction_findings=redaction_findings,
        provider=provider,
        model=model,
        description=description,
        confirm=confirm,
        notify=notify,
        request_kind=request_kind,
    )


async def check_permission(tool_name: str, tool_args: Dict[str, Any], description: str = "") -> bool:
    """
    Convenience function to check permission for a tool.
    
    Args:
        tool_name: Name of the tool
        tool_args: Arguments being passed to the tool
        description: Human-readable description of the action
        
    Returns:
        True if approved (or no manager/auto-approve), False if denied
    """
    manager = get_permission_manager()
    if manager is None:
        return True
    
    return await manager.request_permission(tool_name, tool_args, description)


def check_permission_sync(
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str = "",
    timeout: int = 360,
) -> bool:
    """Synchronously check permission from sync tool functions."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            context = contextvars.copy_context()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: context.run(
                        asyncio.run,
                        check_permission(tool_name, tool_args, description),
                    )
                )
                return bool(future.result(timeout=timeout))
        return bool(asyncio.run(check_permission(tool_name, tool_args, description)))
    except RuntimeError:
        return bool(asyncio.run(check_permission(tool_name, tool_args, description)))
    except Exception as exc:
        logger.error("[ExternalLLMPermission] Permission check failed: %s", exc)
        return False
