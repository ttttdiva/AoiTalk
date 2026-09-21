"""Browser task data and settings; no isolated-browser policy."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .jev_decision_service import JEV_DEFAULT_MODEL


class BrowserAgentError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BrowserStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "navigate",
        "click",
        "type",
        "select",
        "check",
        "uncheck",
        "scroll",
        "back",
        "wait",
        "read",
        "done",
    ]
    instruction: str = Field(default="", max_length=1200)
    value: str = Field(default="", max_length=16000)
    url: str = Field(default="", max_length=8192)
    expected_text: str = Field(default="", max_length=1200)
    expected_url: str = Field(default="", max_length=8192)

    @model_validator(mode="after")
    def validate_action(self):
        if (
            self.action in {"type", "select", "click", "check", "uncheck"}
            and not self.instruction.strip()
        ):
            raise ValueError("an element operation requires a target description")
        if self.action == "type" and "value" not in self.model_fields_set:
            raise ValueError(
                "type requires an explicit value; use an empty string to clear a field"
            )
        if self.action == "navigate" and not self.url:
            raise ValueError("navigate requires url")
        if self.action == "select" and not self.value:
            raise ValueError("select requires an option label")
        if self.action == "scroll" and self.value not in {"", "up", "down"}:
            raise ValueError("scroll requires up or down")
        if self.action == "wait":
            seconds = float(self.value or "0.5")
            if not 0 <= seconds <= 30:
                raise ValueError("wait must be between 0 and 30 seconds")
        return self


class BrowserPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_url: str = Field(default="", max_length=8192)
    goal: str = Field(min_length=1, max_length=16000)
    steps: list[BrowserStep] = Field(min_length=1, max_length=60)
    completion_text: str = Field(default="", max_length=1200)
    completion_url: str = Field(default="", max_length=8192)


class BrowserAgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    jev_enabled: bool = True
    max_steps: int = Field(default=24, ge=1, le=60)
    timeout_seconds: int = Field(default=300, ge=15, le=900)
    jev_model: str = JEV_DEFAULT_MODEL

    @classmethod
    def from_config(cls, config: Any) -> "BrowserAgentSettings":
        value = (
            config.get("browser_agent", {})
            if isinstance(config, Mapping) or callable(getattr(config, "get", None))
            else {}
        )
        return cls.model_validate(value or {})


def browser_settings_payload(config: Any) -> dict[str, Any]:
    return BrowserAgentSettings.from_config(config).model_dump()


def validate_browser_settings_update(value: Any) -> dict[str, Any]:
    try:
        return BrowserAgentSettings.model_validate(value).model_dump()
    except ValueError:
        raise ValueError("ブラウザ操作設定が不正です") from None
