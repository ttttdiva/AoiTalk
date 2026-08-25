"""API ルート間で共有する Pydantic ペイロード定義 (server.py から移設)"""

import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

class ChatMessage(BaseModel):
    type: str  # 'user', 'assistant', 'system'
    message: str
    timestamp: str
    character: Optional[str] = None


class UserMessage(BaseModel):
    message: str
    client_message_id: Optional[str] = Field(default=None, max_length=512)
    agent_run_id: Optional[str] = None


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
    message: str = Field(max_length=131_072)
    # Structured @mentions are carried through REST, durable outbox and the
    # WebSocket worker.  The resolver re-checks every ID; ``name`` is display
    # only and never an identity source.
    mentions: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=32)
    project_id: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    generation_profile: Optional[str] = None
    planning_policy: Optional[str] = None
    include_project_context: bool | None = None
    edit_message_id: Optional[str] = None
    response_model: Optional[ResponseModelSelection] = None
    client_message_id: Optional[str] = Field(default=None, max_length=512)
    command_capabilities: Optional[List[str]] = None
    tools_required: Optional[bool] = None
    skip_user_persistence: bool = False
    persisted_user_message_id: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        max_length=8,
    )
    attachment_context: Optional[str] = Field(
        default=None,
        max_length=524_288,
    )

    @model_validator(mode="after")
    def validate_attachment_sizes(self):
        serialized_attachments = json.dumps(
            self.attachments or [],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized_attachments) > 10_485_760:
            raise ValueError("attachments payload is too large")
        total_data_url_chars = 0
        for attachment in self.attachments or []:
            for key, value in attachment.items():
                if not isinstance(value, str):
                    continue
                if key == "data_url":
                    if len(value) > 4_194_304:
                        raise ValueError("attachment data_url is too large")
                    total_data_url_chars += len(value)
                elif len(value) > 8_192:
                    raise ValueError(f"attachment field is too large: {key}")
        if total_data_url_chars > 8_388_608:
            raise ValueError("total attachment data_url size is too large")
        return self


def effective_include_project_context(
    *,
    message: str,
    requested: bool | None,
    app_context_selected: bool = False,
    attachment_present: bool = False,
    project_selected: bool = False,
) -> bool:
    """Resolve the turn's Project scope.

    An explicit OFF value is authoritative.  Older callers may omit the
    toggle (``None``), in which case only structured app/attachment scope
    selects Project Context.  Ordinary message wording is never interpreted
    as an implicit scope or capability request.
    """
    if requested is not None:
        return bool(requested)
    return bool(app_context_selected or attachment_present)


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
    feature: Optional[str] = None
    enabled: Optional[bool] = None
    features: Optional[Dict[str, bool]] = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_runtime_feature_patch(self):
        has_single = self.feature is not None or self.enabled is not None
        has_group = self.features is not None
        if has_single == has_group:
            raise ValueError("provide either feature/enabled or features")
        if has_single and (not self.feature or self.enabled is None):
            raise ValueError("feature and enabled are both required")
        if has_group and not self.features:
            raise ValueError("features must not be empty")
        return self

    def changes(self) -> Dict[str, bool]:
        if self.features is not None:
            return dict(self.features)
        assert self.feature is not None and self.enabled is not None
        return {self.feature: self.enabled}


class LoginPayload(BaseModel):
    username: str = Field(max_length=255)
    password: str = Field(max_length=1024)


class CreateUserPayload(BaseModel):
    """Payload for creating a new user (admin only)"""

    model_config = ConfigDict(extra="forbid")

    username: StrictStr = Field(min_length=1, max_length=100)
    password: StrictStr = Field(min_length=6, max_length=1024)
    email: Optional[StrictStr] = Field(default=None, max_length=255)
    display_name: Optional[StrictStr] = Field(default=None, max_length=100)
    role: Literal["admin", "user"] = "user"
    require_password_change: StrictBool = True


class UpdateUserPayload(BaseModel):
    """Payload for updating user details"""

    model_config = ConfigDict(extra="forbid")

    email: Optional[StrictStr] = Field(default=None, max_length=255)
    display_name: Optional[StrictStr] = Field(default=None, max_length=100)
    role: Optional[Literal["admin", "user"]] = None
    is_active: Optional[StrictBool] = None
    is_password_reset_required: Optional[StrictBool] = None
    preferred_character: Optional[StrictStr] = Field(default=None, max_length=100)
    user_settings: Optional[Dict[str, Any]] = None


class ChangePasswordPayload(BaseModel):
    """Payload for changing password"""

    model_config = ConfigDict(extra="forbid")

    current_password: Optional[StrictStr] = None  # Required for non-admin users
    new_password: StrictStr = Field(min_length=6, max_length=1024)


class ResetPasswordPayload(BaseModel):
    """One-time password reset token completion payload."""

    model_config = ConfigDict(extra="forbid")

    token: StrictStr = Field(min_length=1, max_length=8192)
    password: StrictStr = Field(min_length=6, max_length=1024)


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
