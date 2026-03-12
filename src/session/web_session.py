"""
Web session management for multi-user support
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from fastapi import WebSocket
from .base import BaseSession


class WebSession(BaseSession):
    """Web interface session with user isolation"""
    
    def __init__(
        self,
        user_id: str,
        username: str,
        role: str = 'user',
        character: Optional[str] = None,
        websocket: Optional[WebSocket] = None
    ):
        """Initialize web session
        
        Args:
            user_id: Authenticated user ID (UUID string)
            username: Username for display
            role: User role ('admin', 'user')
            character: Selected character name
            websocket: WebSocket connection (optional, can be set later)
        """
        super().__init__(
            mode="web",
            character=character
        )
        
        self.user_id = user_id
        self.username = username
        self.role = role
        self.websocket: Optional[WebSocket] = websocket
        
        # Per-user chat history (isolated from other users)
        self.chat_history: List[Dict[str, Any]] = []
        self.max_history = 100
        
        # Per-user LLM context
        self.llm_context: Dict[str, Any] = {
            'conversation_history': [],
            'current_character': character
        }
        
        # Duplicate prevention per session
        self._last_message = ""
        self._last_message_time = 0
        self._duplicate_threshold = 2.0  # seconds
        
    async def initialize(self) -> bool:
        """Initialize session
        
        Returns:
            bool: Always True for web sessions
        """
        self.is_active = True
        return True
    
    async def cleanup(self):
        """Cleanup session resources"""
        self.is_active = False
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.websocket = None
        self.chat_history.clear()
        self.llm_context.clear()
    
    def get_user_info(self) -> Dict[str, Any]:
        """Get user info for session
        
        Returns:
            Dict with user info
        """
        return {
            'user_id': self.user_id,
            'username': self.username,
            'role': self.role,
            'mode': 'web'
        }
    
    def set_websocket(self, websocket: WebSocket):
        """Set or update WebSocket connection
        
        Args:
            websocket: Active WebSocket connection
        """
        self.websocket = websocket
        self.update_activity()
    
    def add_to_history(self, entry: Dict[str, Any]):
        """Add message to chat history
        
        Args:
            entry: Chat message entry
        """
        self.chat_history.append(entry)
        if len(self.chat_history) > self.max_history:
            self.chat_history = self.chat_history[-self.max_history:]
        self.update_activity()
    
    def clear_history(self):
        """Clear chat history"""
        self.chat_history.clear()
        self.llm_context['conversation_history'] = []
        self.update_activity()
    
    def get_history(self) -> List[Dict[str, Any]]:
        """Get chat history
        
        Returns:
            List of chat entries
        """
        return self.chat_history.copy()
    
    def is_duplicate_message(self, message: str) -> bool:
        """Check if message is a duplicate (for voice input)
        
        Args:
            message: Message to check
            
        Returns:
            bool: True if duplicate
        """
        import time
        current_time = time.time()
        
        if (message == self._last_message and 
            current_time - self._last_message_time < self._duplicate_threshold):
            return True
        
        self._last_message = message
        self._last_message_time = current_time
        return False
    
    def update_llm_context(self, role: str, content: str):
        """Update LLM conversation context
        
        Args:
            role: Message role ('user', 'assistant', 'system')
            content: Message content
        """
        self.llm_context['conversation_history'].append({
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })
        
        # Keep only last N messages for context
        max_context = 20
        if len(self.llm_context['conversation_history']) > max_context:
            self.llm_context['conversation_history'] = \
                self.llm_context['conversation_history'][-max_context:]
    
    def get_llm_conversation_history(self) -> List[Dict[str, str]]:
        """Get LLM-ready conversation history
        
        Returns:
            List of messages for LLM input
        """
        return [
            {'role': msg['role'], 'content': msg['content']}
            for msg in self.llm_context.get('conversation_history', [])
        ]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary
        
        Returns:
            Session info dict
        """
        base_dict = super().to_dict()
        base_dict.update({
            'user_id': self.user_id,
            'username': self.username,
            'role': self.role,
            'has_websocket': self.websocket is not None,
            'history_count': len(self.chat_history)
        })
        return base_dict
