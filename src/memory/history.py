"""
Conversation History Management Module
"""

from typing import List, Dict, Any, Optional

class HistoryManager:
    """
    Manages conversation history with automatic truncation.
    
    Attributes:
        max_history_length (int): Maximum number of messages to store.
        context_window_size (int): Number of messages to return for LLM context.
    """
    
    def __init__(self, max_history_length: int = 20, context_window_size: int = 10):
        """
        Initialize HistoryManager.
        
        Args:
            max_history_length: Maximum number of messages to keep in memory.
            context_window_size: Number of messages to use for context generation.
        """
        self.history: List[Dict[str, Any]] = []
        self.max_history_length = max_history_length
        self.context_window_size = context_window_size
        self.summary: str = ""
        self.hard_limit = 100  # Safety limit preventing memory leaks if summarization fails
        
    def add_message(self, role: str, content: str, **kwargs) -> None:
        """
        Add a message to the history.
        
        Args:
            role: Message role (e.g., 'user', 'assistant').
            content: Message content.
            **kwargs: Additional metadata to store with the message.
        """
        message = {
            'role': role,
            'content': content,
            **kwargs
        }
        self.history.append(message)
        self._enforce_hard_limit()
        
    def _enforce_hard_limit(self) -> None:
        """Truncate history to hard_limit to prevent memory leaks."""
        if len(self.history) > self.hard_limit:
            self.history = self.history[-self.hard_limit:]
            
    def get_context(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get recent messages for context.
        
        Args:
            limit: Override default context window size.
            
        Returns:
            List of message dictionaries.
        """
        window_size = limit if limit is not None else self.context_window_size
        if not self.history:
            return []
        return self.history[-window_size:]
    
    def get_context_as_text(self, limit: Optional[int] = None) -> str:
        """
        Get recent messages formatted as text context.
        
        Args:
            limit: Override default context window size.
            
        Returns:
            Formatted string representation of context.
        """
        messages = self.get_context(limit)
        if not messages:
            return ""
            
        # If there's only one message (the current input usually comes separately in some flows, 
        # but here we assume history contains it), just return content if it's the only thing.
        # But typically this is used for "past conversation".
        
        context_parts = []
        # Add summary if exists
        if self.summary:
            context_parts.append(f"【これまでの会話の要約】\n{self.summary}\n")

        for msg in messages:
            role_display = "ユーザー" if msg['role'] == 'user' else "アシスタント"
            context_parts.append(f"{role_display}: {msg['content']}")
            
        return "\n".join(context_parts)

    def update_summary(self, new_summary: str) -> None:
        """Update conversation summary."""
        self.summary = new_summary

    def pop_oldest(self, count: int) -> List[Dict[str, Any]]:
        """
        Remove and return the oldest count messages.
        
        Args:
            count: Number of messages to pop.
            
        Returns:
            List of popped messages.
        """
        if count <= 0:
            return []
            
        popped = self.history[:count]
        self.history = self.history[count:]
        return popped

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all stored messages."""
        return self.history.copy()
        
    def clear(self) -> None:
        """Clear all history."""
        self.history = []
