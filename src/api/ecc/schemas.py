"""ECC API の Pydantic リクエストモデル定義。"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── 統合キャラクター ──


class CreateCharacterRequest(BaseModel):
    name: str
    slug: str
    character_type: str = "assistant"
    system_prompt: str = ""
    model: str = ""
    allowed_tools: List[str] = []
    # 音声（既存 UpdateCharacterRequest と同じ fields を作成時にも受理する）
    voice_engine: str = ""
    voice_name: str = ""
    voice_id: str = ""
    speaker_id: Optional[int] = None
    voice_parameters: dict = {}
    # 性格
    greeting: str = ""
    invalid_content_reply: str = ""
    fallback_reply: str = ""
    goodbye_reply: str = ""
    recognition_aliases: List[str] = []
    # ロールプレイ
    description: str = ""
    personality_summary: str = ""
    first_message: str = ""
    alternate_greetings: List[str] = []
    example_messages: str = ""
    scenario: str = ""
    # RP画像自動生成
    auto_image_gen: bool = False
    image_gen_trigger: Literal["scene_change", "every_n", "emotion_change"] = "scene_change"
    image_gen_interval: int = Field(default=5, ge=1)
    # 外見・画像生成
    appearance_tags: str = ""
    negative_tags: str = ""
    image_gen_engine: Literal["", "comfyui"] = ""
    comfyui_config: dict = {}
    avatar_image_path: str = ""

    @field_validator("image_gen_engine", mode="before")
    @classmethod
    def reject_unsupported_image_engine(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "gemini":
            raise ValueError("image_gen_engine=gemini はサポートされていません")
        if normalized in {"", "comfyui"}:
            return normalized
        raise ValueError(f"未対応の image_gen_engine です: {value}")


class ContextBuildPreviewRequest(BaseModel):
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    message: str = ""
    max_chars: int = Field(default=12000, ge=1000, le=30000)
    is_enabled: bool = True
    # 音声
    voice_engine: str = ""
    voice_name: str = ""
    voice_id: str = ""
    speaker_id: Optional[int] = None
    voice_parameters: dict = {}
    # 性格
    greeting: str = ""
    invalid_content_reply: str = ""
    fallback_reply: str = ""
    goodbye_reply: str = ""
    recognition_aliases: List[str] = []
    # ロールプレイ
    description: str = ""
    personality_summary: str = ""
    first_message: str = ""
    alternate_greetings: List[str] = []
    example_messages: str = ""
    scenario: str = ""
    # 外見・画像生成
    appearance_tags: str = ""
    negative_tags: str = ""
    image_gen_engine: str = ""
    comfyui_config: dict = {}
    avatar_image_path: str = ""


class UpdateCharacterRequest(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    character_type: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    is_enabled: Optional[bool] = None
    # 音声
    voice_engine: Optional[str] = None
    voice_name: Optional[str] = None
    voice_id: Optional[str] = None
    speaker_id: Optional[int] = None
    voice_parameters: Optional[dict] = None
    # 性格
    greeting: Optional[str] = None
    invalid_content_reply: Optional[str] = None
    fallback_reply: Optional[str] = None
    goodbye_reply: Optional[str] = None
    recognition_aliases: Optional[List[str]] = None
    # ロールプレイ
    description: Optional[str] = None
    personality_summary: Optional[str] = None
    first_message: Optional[str] = None
    alternate_greetings: Optional[List[str]] = None
    example_messages: Optional[str] = None
    scenario: Optional[str] = None
    # 外見・画像生成
    appearance_tags: Optional[str] = None
    negative_tags: Optional[str] = None
    image_gen_engine: Optional[Literal["", "comfyui"]] = None
    comfyui_config: Optional[dict] = None
    avatar_image_path: Optional[str] = None
    # RP画像自動生成
    auto_image_gen: Optional[bool] = None
    image_gen_trigger: Optional[Literal["scene_change", "every_n", "emotion_change"]] = None
    image_gen_interval: Optional[int] = Field(default=None, ge=1)

    @field_validator("image_gen_engine", mode="before")
    @classmethod
    def reject_unsupported_image_engine(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized == "gemini":
            raise ValueError("image_gen_engine=gemini はサポートされていません")
        if normalized in {"", "comfyui"}:
            return normalized
        raise ValueError(f"未対応の image_gen_engine です: {value}")


# ── 5. スキル拡張 ──


class CreateCategoryRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    icon: str = ""
    color: str = ""
    sort_order: int = 0


class CreateChainRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    steps: List[dict]


class UpdateChainRequest(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[List[dict]] = None


class ExecuteChainRequest(BaseModel):
    input: str = ""
    parameters: dict = {}


# ── 7. 品質検証 ──


class VerifyRequest(BaseModel):
    user_input: str
    response: str
    context: Optional[str] = None


class UpdateQualityConfigRequest(BaseModel):
    enabled: bool


# ── ワールドブック ──


class CreateWorldBookRequest(BaseModel):
    scenario_id: Optional[str] = None
    name: str
    description: str = ""
    is_enabled: bool = True


class UpdateWorldBookRequest(BaseModel):
    scenario_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None


class CreateEntryRequest(BaseModel):
    name: str = ""
    keywords: List[str] = []
    secondary_keywords: List[str] = []
    content: str
    is_enabled: bool = True
    priority: int = 0
    case_sensitive: bool = False
    constant: bool = False
    insertion_position: str = "before_scenario"


class UpdateEntryRequest(BaseModel):
    name: Optional[str] = None
    keywords: Optional[List[str]] = None
    secondary_keywords: Optional[List[str]] = None
    content: Optional[str] = None
    is_enabled: Optional[bool] = None
    priority: Optional[int] = None
    case_sensitive: Optional[bool] = None
    constant: Optional[bool] = None
    insertion_position: Optional[str] = None


class LinkCharacterRequest(BaseModel):
    character_id: str


# ── Irodori 参照音声 ──


class VoiceAssetOrderRequest(BaseModel):
    """登録済み asset id の新しい順序。"""

    asset_ids: List[str]


class VoiceCaptureStartRequest(BaseModel):
    """WASAPI render-loopback の録音開始指定。"""

    device_id: Optional[str] = None
    # 旧クライアント向けに index も任意受理する。新しい UI は device_id
    # を正本として送信し、バックエンドは開始時にデバイスを再列挙する。
    device_index: Optional[int] = None


class VoicePreviewRequest(BaseModel):
    """Irodori 試聴テキストと編集中設定の任意上書き。"""

    text: str
    caption: Optional[str] = None
    # Character settings are often edited locally before the save request.  A
    # preview can therefore carry the selector directly without mutating the
    # persisted ``voice_parameters`` JSON.
    irodori_model: Optional[str] = None
