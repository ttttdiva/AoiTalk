"""ECC API の Pydantic リクエストモデル定義。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ── 統合キャラクター ──


class CreateCharacterRequest(BaseModel):
    name: str
    slug: str
    character_type: str = "assistant"
    system_prompt: str = ""
    model: str = ""
    allowed_tools: List[str] = []


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
    image_gen_engine: Optional[str] = None
    comfyui_config: Optional[dict] = None
    avatar_image_path: Optional[str] = None


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
