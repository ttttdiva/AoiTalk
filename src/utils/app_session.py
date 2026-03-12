"""
App Session ID Management

Provides a global session ID that persists for the lifetime of the application.
The session ID is derived from the log file timestamp (YYYYMMDD_HHMMSS format),
allowing developers to correlate feedback with specific log files.
"""

from typing import Optional

_current_session_id: Optional[str] = None


def set_session_id(session_id: str) -> None:
    """Set the global application session ID.
    
    This should be called once during application startup, using the same
    timestamp used for the log filename.
    
    Args:
        session_id: Session ID string (typically YYYYMMDD_HHMMSS format)
    """
    global _current_session_id
    _current_session_id = session_id


def get_session_id() -> Optional[str]:
    """Get the current application session ID.
    
    Returns:
        The session ID if set, None otherwise.
    """
    return _current_session_id
