from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CONTRACT_VERSION = "1.0"
CONTRACT_VERSION_HEADER = "AoiTalk-Contract-Version"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobStage = Literal[
    "queued",
    "transcribing",
    "generating_minutes",
    "generating_memo",
    "persisting_minutes",
    "persisting_memo",
    "complete",
]


class MeetingDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_hint: str | None = Field(default=None, max_length=240)
    started_at: datetime | None = None
    participants: list[str] = Field(default_factory=list, max_length=100)
    language: str | None = Field(default=None, max_length=32)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("title_hint", "language")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("participants")
    @classmethod
    def validate_participants(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            participant = str(value).strip()
            if not participant:
                continue
            if len(participant) > 160:
                raise ValueError("participant is too long")
            normalized.append(participant)
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, values: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key).strip()
            value = str(raw_value)
            if not key:
                raise ValueError("metadata key must not be empty")
            if len(key) > 64:
                raise ValueError("metadata key is too long")
            if len(value) > 500:
                raise ValueError("metadata value is too long")
            result[key] = value
        return result


class MeetingProcessingClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["28_MeetingMinutes"]
    version: str | None = Field(default=None, max_length=64)


class MeetingProcessingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    meeting: MeetingDescriptor = Field(default_factory=MeetingDescriptor)
    client: MeetingProcessingClient


class MeetingProcessingError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    stage: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class MeetingProcessingErrorEnvelope(BaseModel):
    """FastAPI ``HTTPException`` wire envelope for this API."""

    model_config = ConfigDict(extra="forbid")

    detail: MeetingProcessingError


class MeetingAudioInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str


class MeetingTranscriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    language: str | None = None
    duration_seconds: float | None = None


class MeetingDocumentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    markdown: str
    docs_node_id: str


class MeetingProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: MeetingTranscriptResult
    minutes: MeetingDocumentResult
    memo: MeetingDocumentResult


class MeetingProcessingJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    job_id: str
    status: JobStatus
    stage: JobStage
    idempotency_key: str
    retry_generation: int
    attempt_count: int
    retryable: bool
    audio: MeetingAudioInfo
    result: MeetingProcessingResult | None = None
    error: MeetingProcessingError | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime


class MeetingProcessingHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Literal["meeting-processing"] = "meeting-processing"
    status: Literal["ok"] = "ok"
    contract_versions: list[str] = Field(
        default_factory=lambda: [CONTRACT_VERSION]
    )


class MeetingProcessingReadinessChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: bool
    worker: bool
    whisper: bool
    local_llm: bool
    docs: bool


class MeetingProcessingLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_audio_bytes: int
    max_duration_seconds: int
    allowed_suffixes: list[str]


class MeetingProcessingReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    ready: bool
    checks: MeetingProcessingReadinessChecks
    limits: MeetingProcessingLimits
