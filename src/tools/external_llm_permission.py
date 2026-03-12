"""
External LLM Permission Manager

Manages user permission requests for external LLM API calls (e.g., web_search, grok_x_search).
When auto_approve is disabled in config, sends permission requests to WebUI and waits for user response.
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum

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
        self.auto_approve = True  # Default to current behavior
        self.enabled_tools = ["web_search", "grok_x_search"]
        
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
        # Auto-approve if configured
        if self.auto_approve:
            return True
        
        # Check if tool requires permission
        if tool_name not in self.enabled_tools:
            return True
        
        # Require broadcast callback
        if self._broadcast_callback is None:
            logger.warning("[ExternalLLMPermission] No broadcast callback set, auto-approving")
            return True
        
        # Create request
        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        
        request = PermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description or self._generate_description(tool_name, tool_args),
            future=future
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
            return True  # Fail open to maintain functionality
        finally:
            # Clean up
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
            request.future.set_result(approved)
        
        logger.info(f"[ExternalLLMPermission] Permission response: {request_id} -> {'approved' if approved else 'denied'}")
    
    def _generate_description(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Generate a human-readable description of the action"""
        descriptions = {
            "web_search": lambda args: f"Web検索: 「{args.get('query', '')}」",
            "grok_x_search": lambda args: f"X (Twitter) 検索: 「{args.get('query', '')}」"
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
