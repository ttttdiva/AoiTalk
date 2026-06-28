"""API ルート間で共有する Pydantic ペイロード定義 (server.py から移設)"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ...llm.tool_policy import looks_like_project_management_request


class ChatMessage(BaseModel):
    type: str  # 'user', 'assistant', 'system'
    message: str
    timestamp: str
    character: Optional[str] = None


class UserMessage(BaseModel):
    message: str


class VoiceStatus(BaseModel):
    ready: bool
    rms: float
    recording: bool


class MobileCommandRequest(BaseModel):
    command_id: str


class ResponseModelSelection(BaseModel):
    provider: str
    model: str


class ConversationDispatchRequest(BaseModel):
    message: str
    project_id: Optional[str] = None
    generation_profile: Optional[str] = None
    include_project_context: bool = False
    edit_message_id: Optional[str] = None
    response_model: Optional[ResponseModelSelection] = None
    client_message_id: Optional[str] = None
    command_capabilities: Optional[List[str]] = None
    skip_user_persistence: bool = False
    persisted_user_message_id: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None
    attachment_context: Optional[str] = None


def effective_include_project_context(
    *,
    message: str,
    requested: bool,
) -> bool:
    """Force project context for requests that explicitly need project evidence."""
    return bool(requested or looks_like_project_management_request(message or ""))


def sanitize_response_model_selection(value: Any) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if isinstance(value, ResponseModelSelection):
        raw_provider = value.provider
        raw_model = value.model
    elif isinstance(value, dict):
        raw_provider = value.get("provider")
        raw_model = value.get("model")
    else:
        return None

    provider = str(raw_provider or "").strip()
    model = str(raw_model or "").strip()
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


class RuntimeFeaturePatchPayload(BaseModel):
    feature: str
    enabled: bool


class LoginPayload(BaseModel):
    username: str
    password: str


class CreateUserPayload(BaseModel):
    """Payload for creating a new user (admin only)"""

    username: str
    password: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: str = "user"  # 'admin' or 'user'


class UpdateUserPayload(BaseModel):
    """Payload for updating user details"""

    email: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    preferred_character: Optional[str] = None
    user_settings: Optional[dict] = None


class ChangePasswordPayload(BaseModel):
    """Payload for changing password"""

    current_password: Optional[str] = None  # Required for non-admin users
    new_password: str


class CrawlerStatusReport(BaseModel):
    """クローラーからのステータスレポート

    クローラーは追加のフィールド（processed_servers, processed_channels等）を
    送信する場合があるため、extra='allow'で受け入れる。
    """

    model_config = {"extra": "allow"}

    name: str  # クローラー名（例: "VideoCrawler", "DiscordCrawler"）
    status: str  # "running", "idle", "error"
    details: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


class DocumentContent(BaseModel):
    content: str


class SettingsPayload(BaseModel):
    """Payload for updating a configuration setting"""

    key: str  # e.g., "tts.speed_adjustment"
    value: Any
    persist: bool = True  # Whether to save to DB


class OllamaPullPayload(BaseModel):
    """Payload for starting an Ollama model pull."""

    model: str


class OllamaModelPayload(BaseModel):
    """Payload for Ollama model operations."""

    model: str
